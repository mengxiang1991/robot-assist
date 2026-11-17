# Robot Assist 个人助手

运行版 Web 控制台使用 Python 3.12+ 标准库和 SQLite。原型设计稿见 assistant-design.html。

## 本机开发

在项目目录执行以下命令。三个命令分别初始化会议室样例、启动 Worker 和启动 Web。后两个命令各占用一个终端。

```powershell
python -m orbit.demo
python -m orbit worker
python -m orbit api
```

访问 http://127.0.0.1:8780 。配置、计划及运行数据默认写入 ./data。可用 ORBIT_DATA 指向其他**本机**目录，API、Worker 和 demo 三者必须使用同一路径。

打开「功能库 · 会议室」后填写日期、时段和房间，执行预演。在运行详情看到房间可用后点击确认，Worker 会重新检查可用性。批量模式为每个日期生成独立预演，可在运行记录逐项确认。重发同一个审批请求返回同一个提交运行。样例在 SQLite 中写入记录。

## 配置格式

界面「模型、工具和 Flow」保存命名配置。配置 ID 全局唯一。以下 JSON 中的 key 是示例配置 ID，value 是对应配置内容。

```json
{
  "my-model": {
    "url": "https://provider.example/v1",
    "model": "provider-model-id",
    "token_env": "ORBIT_MODEL_KEY",
    "timeout": 45
  },
  "knowledge-query": {
    "type": "mcp",
    "transport": "http",
    "url": "https://mcp.example/mcp",
    "operation": "search_docs",
    "token_env": "ORBIT_MCP_KEY",
    "effect": "read",
    "schema": {"type": "object", "properties": {"query": {"type": "string"}}}
  },
  "weekly-digest": {
    "name": "知识库摘要",
    "executor": "agent",
    "model": "my-model",
    "tools": ["knowledge-query"],
    "instruction": "用知识库查询工具收集材料，再生成有出处的摘要。",
    "max_turns": 4
  }
}
```

固定 Flow 使用 executor=flow 和 steps，节点依次接收前一节点的输出。model 节点包含 instruction，可用 json=true 要求返回可解析的 JSON。tool 节点包含 tool（配置 ID），可用 arguments 指定固定输入。写工具只在运行参数 commit=true 或审批后的提交任务执行。配置 effect 必须按工具实际副作用填写。

CLI 或现有 Skill 可使用一个 JSON stdin/stdout 适配脚本。配置 type 为 cli 或 skill，command 是 Worker 机器上的绝对可执行路径，args 是固定参数数组，cwd 是可选工作目录，env 是需要透传的环境变量名称。对目录式 Skill，可让已有运行器或自定义桥接程序接收 JSON，再调用原 Skill。MCP stdio 同样使用 command 和 args，并额外设置 operation、transport=stdio。工具的模型 Schema 用来向模型声明参数，业务参数校验应由工具自身实现。

计划当前使用每日 HH:MM 和 IANA 时区。计划创建时保存 Flow、工具及模型配置快照，修改配置后重新保存计划即可切换。可用 input.date_offset_days 表示从触发日期起多少天，例如明天为 1；显式日期优先。停机错过的分钟跳过。定时执行与 Worker 主执行循环独立。

## 用你的会议室 Skill 创建提示词预订

在配置中心登记模型及两个 Skill 操作：只读查询和提交。新建预订 Flow，设置 `approval: "booking"` 和 `executor: "flow"`。节点可以依次为：

1. `model`，启用 `json: true`，提示模型将输入的 prompt 解析为 Skill 所需的字段。手工或批量提供的显式日期优先。补充参考日期和时区，避免相对日期歧义。
2. `tool`，调用只读查询。适配器返回包含 `available: true` 的候选对象，附带可提交的绝对日期、时段和房间 ID。
3. `tool`，调用写工具，从查询结果取得参数。实际提交时工具应重新核对可用性。预演跳过此节点，确认时使用之前选定的候选对象。

在「功能库 · 会议室」选择这个 Flow，再输入提示词。样例 Flow 使用结构化日期，不调用模型。实际接入时按现有 Skill 的返回结构实现适配器。

## 接入已有市场

市场 URL 指向返回 JSON 的目录入口，认证可配置 token_env。目录最小格式如下。真实市场字段或认证有差异时，在 orbit/__main__.py 中的市场接口处适配，接入后在 UI 浏览并将选定版本导入为工具配置草稿。

```json
{
  "items": [
    {
      "id": "knowledge-search",
      "name": "知识库",
      "version": "1.0.0",
      "tool": {
        "type": "mcp",
        "transport": "http",
        "url": "https://mcp.example/mcp",
        "operation": "search_docs",
        "effect": "read"
      }
    }
  ]
}
```

当前导入 MCP 定义或可运行 Skill 的描述，Skill 包由 Worker 环境安装。只添加自己信任的工具及市场。市场凭据放在 API 环境，模型与工具凭据放在 Worker 环境；Web 仅保存环境变量名。需要调用模型连接测试接口时，也给 API 提供对应凭据。

## Docker 与虚机

Docker Compose 需要本机 Docker Engine/Compose。在项目目录创建未纳入版本控制的 .env，填入强随机 ORBIT_WEB_TOKEN 和使用到的凭据变量：

```text
ORBIT_WEB_TOKEN=replace-with-a-long-random-token
```

```sh
docker compose up -d --build
docker compose run --rm --entrypoint python worker -m orbit.demo
```

打开本机页面，点「访问令牌」并填写 ORBIT_WEB_TOKEN。Compose 把控制台端口限制在宿主回环地址。

虚机中把代码发布到 /opt/orbit，创建可写本地数据目录并设 ORBIT_DATA。用同一非 root 账号运行两个 systemd 服务，设置 WorkingDirectory=/opt/orbit、EnvironmentFile 和 Restart=on-failure，然后分别配置 ExecStart=/usr/bin/python3 -m orbit api 和 ExecStart=/usr/bin/python3 -m orbit worker。远程访问通过 TLS 反代并设置 ORBIT_WEB_TOKEN。停服务后再复制 SQLite 数据目录进行冷备份。

生产扩展设计在 assistant-design.html。当前可运行版本集中在单机执行、每日计划、顺序 Flow、受限 Agent 与本地样例。通用审批、发布版本管理、真实 Skill 制品下载、消息 Hook 投递、Cron 和浏览器自动化留在后续实现；事件已经写入数据库供扩展。
