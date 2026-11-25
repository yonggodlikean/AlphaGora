# 参与共建 Agora

感谢你的帮助。Agora 代码量不大，大多数贡献只涉及其中一个服务。
English: [CONTRIBUTING.md](CONTRIBUTING.md)

## 基本原则

- **只做模拟交易**：连接真实券商、下真实订单或宣传收费荐股的 PR 不会被合并。
- **好玩、简单优先**：能让擂台更好玩的小功能，胜过复杂的大功能。
- 在 Issue 和评审里友善相待。有问题欢迎到 [Discussions](https://github.com/x-pai/AlphaGora/discussions) 提问。

## 搭建开发环境

需要 Python 3.12 + [uv](https://github.com/astral-sh/uv)、Rust（stable）、Node 22 和 Docker。

```bash
# 引擎 + API：测试会自带 PostgreSQL（pgserver），不用另外安装
cd services/arena-engine
uv venv -p 3.12 && uv pip install -e ".[api,dev]"
.venv/bin/pytest -q

# 连本地 Postgres、用合成演示行情跑起来
DATABASE_URL=postgresql://agora:agora@127.0.0.1:5432/agora ARENA_MARKET_FEED=demo ARENA_REQUIRE_INVITE=0 \
  .venv/bin/uvicorn arena_engine.api.main:app --reload

# 网页（会把 /api/v1 转发给 ARENA_API_INTERNAL_URL，默认 http://localhost:8000）
cd apps/web && npm install && npm run dev

# 行情网关
cargo test -p market-gateway

# OpenPup 技能和 runner
services/arena-engine/.venv/bin/pytest -q packages/openpup-skill/tests
```

每个 PR 都会由 CI（`.github/workflows/ci.yml`）把上面这些全部跑一遍。

## 可以从哪里入手

| 想法 | 改哪里 |
|---|---|
| 新的官方规则战队（人设） | `arena_engine/personas.py` + `arena_engine/house_bots.py` 里加一个方法 + 测试 |
| OpenPup 或大模型战队 | `packages/openpup-skill/runner/`（提示词、角色、选股逻辑） |
| 新徽章、新的毒舌文案 | `arena_engine/personas.py`（`BADGES`）、`arena_engine/roast.py`，发放逻辑在 `service.py` |
| 界面 | `apps/web/app/*`（Next.js App Router，样式在 `app/globals.css`） |
| 行情数据 | `services/market-gateway`（Rust，rustdx-complete） |
| 撮合规则 | `arena_engine/matching/`（纯逻辑、无 I/O；每条规则都要在 `tests/test_matching_engine.py` 里有测试） |

改引擎之前，请先读 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 代码风格

- **Python**：写类型标注；金额和价格用 `Decimal`，引擎里不做浮点运算。撮合核心不做 I/O，持久化放在 `store.py`，
  调度放在 `service.py`。
- **Rust**：`cargo fmt`，不引入新的 warning。
- **TypeScript**：`strict` 模式，`npx tsc --noEmit` 无报错。沿用 A 股配色（红涨绿跌），并且总是配上 ▲▼ 或正负号。
- 面向用户的文字用中文；代码、注释和提交信息用英文。

## 提交 PR

1. Fork 仓库，从 `main` 拉分支，一个 PR 只做一件事。
2. 补充或更新测试。修 bug 时先写一个"不修就会失败"的测试。
3. 行为变了就更新文档（README、ARCHITECTURE、`.env.example`）。
4. 提交信息：一行简短的祈使句标题（如 "Add limit-down queue badge"），正文说明为什么要改。
5. 不要提交任何密钥、`.env`、证书或私钥（`.gitignore` 已经覆盖了常见的几类）。

## 报告安全问题

请不要公开提 Issue，使用本仓库 GitHub 的「私密漏洞报告」（private vulnerability reporting）。
