using System;
using System.IO;
using System.Linq;
using System.Reflection;

// 静态反射转储 Assembly-CSharp 里 PlortCollector/LandPlot 的成员，
// 供 corral_collect 实现参考。只读元数据，不需要游戏运行。
class DumpPlortCollector
{
    static void Main(string[] args)
    {
        string managed = args.Length > 0 ? args[0]
            : @"E:\SteamLibrary\steamapps\common\Slime Rancher\SlimeRancher_Data\Managed";
        AppDomain.CurrentDomain.ReflectionOnlyAssemblyResolve += (s, e) =>
        {
            string name = new AssemblyName(e.Name).Name;
            string p = Path.Combine(managed, name + ".dll");
            return File.Exists(p) ? Assembly.ReflectionOnlyLoadFrom(p) : null;
        };
        var asm = Assembly.ReflectionOnlyLoadFrom(Path.Combine(managed, "Assembly-CSharp.dll"));
        string[] wants = args.Skip(1).ToArray();
        foreach (var t in asm.GetTypes())
        {
            if (wants.Length > 0 && !wants.Any(w => t.Name.Equals(w, StringComparison.OrdinalIgnoreCase))) continue;
            if (wants.Length == 0 && t.Name.IndexOf("PlortCollector", StringComparison.OrdinalIgnoreCase) < 0) continue;
            Console.WriteLine("=== " + t.FullName + " : " + (t.BaseType != null ? t.BaseType.FullName : "?"));
            const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
            foreach (var f in t.GetFields(F))
            {
                try { Console.WriteLine("  FIELD  " + f.FieldType.Name + " " + f.Name); }
                catch (Exception ex) { Console.WriteLine("  FIELD  ?" + f.Name + " (!" + ex.GetType().Name + ")"); }
            }
            foreach (var p in t.GetProperties(F))
            {
                try { Console.WriteLine("  PROP   " + p.PropertyType.Name + " " + p.Name); }
                catch (Exception ex) { Console.WriteLine("  PROP   ?" + p.Name + " (!" + ex.GetType().Name + ")"); }
            }
            foreach (var m in t.GetMethods(F))
            {
                if (m.IsSpecialName) continue;
                try
                {
                    var ps = string.Join(",", m.GetParameters().Select(x => x.ParameterType.Name + " " + x.Name).ToArray());
                    Console.WriteLine("  METHOD " + m.ReturnType.Name + " " + m.Name + "(" + ps + ")");
                }
                catch (Exception ex) { Console.WriteLine("  METHOD ?" + m.Name + " (!" + ex.GetType().Name + ")"); }
            }
        }
    }
}
