using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using SlimeBridge;

// HTTP 服务端：本机只读端口，暴露 JSON REST。
//   GET  /api/status          -> 返回游戏状态 {money, facilities, plorts, area, ...}
//   POST /api/action          -> body 形如 {"action":"build","kind":"corral"}
//   GET  /api/health          -> 存活探针（插件用 is_alive 轮询）
// 只允许 127.0.0.1，防其他进程调用。

public class SimpleJson
{
    // 手写极简 JSON 编码/解码，避免引第三方库（Unity 环境依赖越少越稳）。
    public static string Esc(string s)
    {
        if (s == null) return "null";
        var sb = new StringBuilder(s.Length + 8);
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default: sb.Append(c); break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }

    // 从 JSON 里按 key 抓字符串值（只处理 {"a":... , "b": "str"} 平铺结构，够用）。
    public static string GetStr(string json, string key)
    {
        if (string.IsNullOrEmpty(json)) return null;
        int i = json.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
        if (i < 0) return null;
        int colon = json.IndexOf(':', i);
        if (colon < 0) return null;
        int q = colon + 1;
        while (q < json.Length && Char.IsWhiteSpace(json[q])) q++;
        if (q >= json.Length || json[q] != '"') return null;
        q++;
        var sb = new StringBuilder();
        while (q < json.Length)
        {
            char c = json[q++];
            if (c == '"') break;
            if (c == '\\' && q < json.Length)
            {
                char n = json[q++];
                if (n == 'n') sb.Append('\n');
                else if (n == 't') sb.Append('\t');
                else if (n == 'r') sb.Append('\r');
                else if (n == 'u' && q + 4 <= json.Length)
                {
                    // \uXXXX -> 真实字符（Python json.dumps 默认 ensure_ascii 会把中文转成这种形式）
                    int code = 0;
                    bool hexOk = true;
                    for (int k = 0; k < 4; k++)
                    {
                        char h = json[q + k];
                        int v;
                        if (h >= '0' && h <= '9') v = h - '0';
                        else if (h >= 'a' && h <= 'f') v = h - 'a' + 10;
                        else if (h >= 'A' && h <= 'F') v = h - 'A' + 10;
                        else { hexOk = false; break; }
                        code = code * 16 + v;
                    }
                    if (hexOk) { sb.Append((char)code); q += 4; }
                    else sb.Append('u');
                }
                else sb.Append(n);
            }
            else sb.Append(c);
        }
        return sb.ToString();
    }
}

public class SlimeBridgeServer
{
    public const int DefaultPort = 8124;
    // mod 版本与能力清单：NEKO 插件通过 /api/health 做能力握手，
    // 旧版 mod 缺少新动作时插件应提示更新而不是静默失败。
    public const string ModVersion = "0.4.0";
    public const string ModFeatures =
        "[\"status\",\"action\",\"scan_target\",\"scan_corral\",\"scan_market\",\"scan_food\"," +
        "\"scan_plort\",\"scan_plot\",\"scan_facilities\",\"ui_buttons\",\"debug_ui\",\"set_overlay\"," +
        "\"tp_market\",\"tp_plot\",\"tp_item\",\"feed_show\"]";
    private readonly HttpListener _listener;
    private Thread _thread;
    private volatile bool _running;
    public IGameFacade Facade { get; set; }   // 由 Plugin 注入：读状态 + 执行动作

    public SlimeBridgeServer(int port)
    {
        _listener = new HttpListener();
        _listener.Prefixes.Add("http://127.0.0.1:" + port + "/");
        _running = false;
        Facade = null;
    }

    public void Start()
    {
        if (_running) return;
        _listener.Start();
        _running = true;
        _thread = new Thread(Loop) { IsBackground = true, Name = "SlimeBridgeHTTP" };
        _thread.Start();
        UMFLog.Log("[SlimeBridge] HTTP server listening on http://127.0.0.1:" + DefaultPort + "/");
    }

    public void Stop()
    {
        _running = false;
        try { _listener.Stop(); } catch (Exception) { }
        try { _listener.Close(); } catch (Exception) { }
    }

    private void Loop()
    {
        while (_running)
        {
            HttpListenerContext ctx;
            try { ctx = _listener.GetContext(); }
            catch (Exception) { break; }   // listener 停止时会抛异常，直接退出线程
            try { Handle(ctx); }
            catch (Exception e) { UMFLog.Log("[SlimeBridge] handler error: " + e); }
        }
    }

    private void Handle(HttpListenerContext ctx)
    {
        string path = ctx.Request.Url.AbsolutePath;
        byte[] body = null;
        int code = 200;

        if (path == "/api/health")
        {
            body = Encoding.UTF8.GetBytes(
                "{\"ok\":true,\"name\":\"slime_bridge\",\"version\":\"" + ModVersion +
                "\",\"features\":" + ModFeatures + "}");
        }
        else if (path == "/api/status")
        {
            var gs = Facade != null ? Facade.ReadStatus() : new GameStatus { money = -1, note = "facade 未就绪" };
            body = Encoding.UTF8.GetBytes(StatusToJson(gs));
        }
        else if (path == "/api/action" && ctx.Request.HttpMethod == "POST")
        {
            string req;
            using (var r = new System.IO.StreamReader(ctx.Request.InputStream, Encoding.UTF8))
                req = r.ReadToEnd();
            string action = (SimpleJson.GetStr(req, "action") ?? "").Trim().ToLowerInvariant();
            string kind = (SimpleJson.GetStr(req, "kind") ?? "").Trim();
            // kind 除 set_overlay（自由文字，如中文状态提示）外均为固定小写关键字，统一转小写
            if (action != "set_overlay") kind = kind.ToLowerInvariant();
            var res = Facade != null ? Facade.ExecuteAction(action, kind) : new ActionResult { ok = false, note = "facade 未就绪" };
            body = Encoding.UTF8.GetBytes(ActionToJson(res));
        }
        else
        {
            code = 404;
            body = Encoding.UTF8.GetBytes("{\"ok\":false,\"note\":\"not found\"}");
        }

        ctx.Response.StatusCode = code;
        ctx.Response.ContentType = "application/json; charset=utf-8";
        ctx.Response.ContentLength64 = body.Length;
        using (var s = ctx.Response.OutputStream)
            s.Write(body, 0, body.Length);
        ctx.Response.Close();
    }

    private static string StatusToJson(GameStatus gs)
    {
        return "{\"ok\":true,\"mode\":\"bridge\",\"game_loaded\":" +
               (gs.gameLoaded ? "true" : "false") +
               ",\"money\":" + (gs.money < 0 ? "null" : gs.money.ToString(CultureInfo.InvariantCulture)) +
               ",\"money_estimated\":" + (gs.moneyEstimated ? "true" : "false") +
               ",\"facilities\":{\"corral\":{\"level\":" + gs.corralLevel + ",\"count\":" + gs.corralCount + "}" +
               ",\"coop\":{\"level\":" + gs.coopLevel + ",\"count\":" + gs.coopCount + "}" +
               ",\"garden\":{\"level\":" + gs.gardenLevel + ",\"count\":" + gs.gardenCount + "}}," +
               "\"plorts\":" + SimpleJson.Esc(gs.plorts) +
               ",\"area\":" + SimpleJson.Esc(gs.area) +
               ",\"note\":" + SimpleJson.Esc(gs.note) + "}";
    }

    private static string ActionToJson(ActionResult ar)
    {
        return "{\"ok\":" + (ar.ok ? "true" : "false") +
               ",\"strategy\":" + SimpleJson.Esc(ar.strategy) +
               ",\"gains\":" + ar.gains.ToString(CultureInfo.InvariantCulture) +
               ",\"count\":" + ar.count.ToString(CultureInfo.InvariantCulture) +
               ",\"caught\":" + SimpleJson.Esc(ar.caught ?? "") +
               ",\"new_area\":" + (ar.newArea ? "true" : "false") +
               ",\"target_kind\":" + SimpleJson.Esc(ar.targetKind ?? "") +
               ",\"dist\":" + ar.dist.ToString(CultureInfo.InvariantCulture) +
               ",\"yaw_delta\":" + ar.yawDelta.ToString(CultureInfo.InvariantCulture) +
               ",\"px\":" + ar.px.ToString(CultureInfo.InvariantCulture) +
               ",\"py\":" + ar.py.ToString(CultureInfo.InvariantCulture) +
               ",\"tx\":" + ar.tx.ToString(CultureInfo.InvariantCulture) +
               ",\"tz\":" + ar.tz.ToString(CultureInfo.InvariantCulture) +
               ",\"cx\":" + ar.cx.ToString(CultureInfo.InvariantCulture) +
               ",\"cz\":" + ar.cz.ToString(CultureInfo.InvariantCulture) +
               ",\"cyaw\":" + ar.cyaw.ToString(CultureInfo.InvariantCulture) +
               ",\"pitch\":" + ar.pitchDelta.ToString(CultureInfo.InvariantCulture) +
               ",\"area\":" + SimpleJson.Esc(ar.area ?? "") +
               ",\"note\":" + SimpleJson.Esc(ar.note ?? "") +
               ",\"payload\":" + SimpleJson.Esc(ar.payload ?? "") + "}";
    }
}