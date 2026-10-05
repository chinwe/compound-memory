# Privacy gate（隐私门禁）

两层自动化门禁，拦「姓名/昵称/本机路径等个人信息进入仓库」。**核心设计：检查逻辑在仓库里，禁串本身永远不在仓库里**——把禁串写进 hook/CI 配置等于先把要保密的内容提交上去。

| 层 | 位置 | 禁串来源 | 行为 |
|---|---|---|---|
| pre-commit（本地，反馈最快） | `scripts/hooks/pre-commit` → `scripts/check-privacy.sh`，经 `git config core.hooksPath scripts/hooks` 启用（该配置只在本地 `.git/config`） | `~/.config/compound-memory/privacy-patterns.txt`（一行一个正则，`#` 注释） | 命中即拒绝提交；patterns 未配置则警告放行；`--no-verify` 可绕过 |
| CI（权威拦截，绕不过） | `.github/workflows/ci.yml` 的 `privacy` job，扫全部 git 追踪文件 | GitHub Actions Secret `PRIVACY_PATTERNS`（竖线分隔正则；Secrets 不入仓库、不进日志、fork PR 不可见） | 命中即红；Secret 未设置（如 fork）警告放行 |

## 一次性配置（本机）

```bash
mkdir -p ~/.config/compound-memory
printf '# 每行一个正则\n<禁串正则，一行一个>\n' > ~/.config/compound-memory/privacy-patterns.txt
git config core.hooksPath scripts/hooks
```

## 更新禁串

本地改 patterns 文件 + 同步更新 Secret：

```bash
gh secret set PRIVACY_PATTERNS --repo chinwe/compound-memory --body "pattern1|pattern2"
```

## 语义约定

- `check-privacy.sh [文件...]`：缺省扫全部追踪文件（CI 用），hook 传暂存文件；grep 出错（退出 2）时门禁拒绝放行——**检查失败不得静默等价于检查通过**。
- 未配置 patterns 时放行而不是报错：门禁的权威层是 CI Secret，本地层只服务维护者；开源协作者的克隆没有 patterns 文件也不该被阻塞。
- 历史（提交对象）里的存量泄露不在此门禁范围内——它只拦增量；历史清理需改写历史（破坏性，另行决策）。
