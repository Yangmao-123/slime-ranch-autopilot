import {
  ActionButton,
  Card,
  KeyValue,
  Page,
  Stack,
  StatusBadge,
  Text,
  Input,
  Button,
  Field,
  DataTable,
  Alert,
} from "@neko/plugin-ui"
import { useLocalState } from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type State = {
  running?: boolean
  mode?: string
  phase?: string
  funds?: number
  discovery?: string
  reports?: string[]
  timeout_seconds?: number
  cycle_seconds?: number
  pid?: number | null
}

export default function Dashboard(props: PluginSurfaceProps<State>) {
  const { state, actions, t } = props
  const start = actions.find((a) => a.id === "start_autopilot") as HostedAction | undefined
  const stop = actions.find((a) => a.id === "stop_autopilot") as HostedAction | undefined
  const setMode = actions.find((a) => a.id === "set_mode") as HostedAction | undefined
  const ref = actions.find((a) => a.id === "set_timeout") as HostedAction | undefined
  const cyc = actions.find((a) => a.id === "set_cycle") as HostedAction | undefined
  const pushCmd = actions.find((a) => a.id === "push_command") as HostedAction | undefined

  const [seconds, setSeconds] = useLocalState("timeout_seconds", state?.timeout_seconds ?? 10)
  const [cycle, setCycle] = useLocalState("cycle_seconds", state?.cycle_seconds ?? 30)
  const [cmdText, setCmdText] = useLocalState("cmd_text", "")

  return (
    <Page title={props.plugin.name} subtitle={t("dashboard.subtitle", { defaultValue: "NEKO 接管史莱姆牧场运营" })}>
      <Card title={t("dashboard.status", { defaultValue: "运行状态" })}>
        <Stack>
          <KeyValue
            items={[
              { key: t("f.running", { defaultValue: "运行中" }), value: <StatusBadge tone={state?.running ? "success" : "muted"}>{state?.running ? "ON" : "OFF"}</StatusBadge> },
              { key: t("f.mode", { defaultValue: "模式" }), value: state?.mode === "ranch" ? t("m.ranch", { defaultValue: "自主经营" }) : t("m.watch", { defaultValue: "观望" }) },
              { key: t("f.phase", { defaultValue: "阶段" }), value: state?.phase ?? "-" },
              { key: t("f.funds", { defaultValue: "资金" }), value: `$${state?.funds ?? 0}` },
              { key: t("f.pid", { defaultValue: "核心PID" }), value: state?.pid != null ? String(state.pid) : "-" },
            ]}
          />
          {state?.discovery ? (
            <Alert tone="info">{state.discovery}</Alert>
          ) : null}
        </Stack>
      </Card>

      <Card title={t("dashboard.control", { defaultValue: "控制" })}>
        <Stack>
          {start ? <ActionButton action={start} label={t("btn.start", { defaultValue: "启动" })} /> : null}
          {stop ? <ActionButton action={stop} tone="danger" label={t("btn.stop", { defaultValue: "停止" })} /> : null}
          {setMode ? (
            <Stack direction="row">
              <ActionButton action={setMode} values={{ mode: "ranch" }} label={t("btn.modeRanch", { defaultValue: "切到自主经营" })} />
              <ActionButton action={setMode} values={{ mode: "watch" }} label={t("btn.modeWatch", { defaultValue: "切到观望" })} />
            </Stack>
          ) : null}
        </Stack>
      </Card>

      <Card title={t("dashboard.config", { defaultValue: "设置" })}>
        <Stack>
          <Field label={t("f.timeout", { defaultValue: "语音无输入接管秒数" })}>
            <Input
              type="number"
              value={String(seconds)}
              onInput={(e) => setSeconds(Number((e.target as HTMLInputElement).value))}
            />
          </Field>
          {ref ? <ActionButton action={ref} values={{ seconds }} label={t("btn.saveTimeout", { defaultValue: "保存秒数" })} /> : null}
          <Field label={t("f.cycle", { defaultValue: "经营循环时间（秒）" })} hint="市场→围栏收结晶→满包回卖→直到清空，一轮隔这么久">
            <Input
              type="number"
              value={String(cycle)}
              onInput={(e) => setCycle(Number((e.target as HTMLInputElement).value))}
            />
          </Field>
          {cyc ? <ActionButton action={cyc} values={{ seconds: cycle }} label={t("btn.saveCycle", { defaultValue: "保存循环时间" })} /> : null}
        </Stack>
      </Card>

      <Card title={t("dashboard.cmd", { defaultValue: "手动指令（测试/远程指挥）" })}>
        <Stack>
          <Field label={t("f.cmd", { defaultValue: "指令文本" })} hint="例：建造菜园 / 探索 / 售卖结晶">
            <Input value={cmdText} onInput={(e) => setCmdText((e.target as HTMLInputElement).value)} />
          </Field>
          {pushCmd ? <Button onClick={() => props.api.call("push_command", { text: cmdText })} >{t("btn.cmd", { defaultValue: "执行" })}</Button> : null}
        </Stack>
      </Card>

      <Card title={t("dashboard.reports", { defaultValue: "新发现汇报（最近12条）" })}>
        {(state?.reports ?? []).length ? (
          <DataTable
            data={(state?.reports ?? []).map((text, i) => ({ id: i, text }))}
            rowKey="id"
            columns={[{ key: "text", label: t("f.report", { defaultValue: "内容" }) }]}
          />
        ) : (
          <Text>{t("dashboard.noReports", { defaultValue: "暂无新发现汇报" })}</Text>
        )}
      </Card>
    </Page>
  )
}