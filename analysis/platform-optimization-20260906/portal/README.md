# 质量门户

线上构建后的 `portal-dist`、失效分析 `failure-dist` 和机组运行 `machine-watch-dist` 已保留；本机同时发现并迁入了可再编译的前端工程源码：`frontend/` 是质量门户，`failure-frontend/` 是失效分析前端。两套工程均保留 `package.json`、`package-lock.json`、TypeScript/Vite 配置、`src/` 和必要静态资源，但不含 `node_modules`、构建目录或截图。

```bash
(cd frontend && npm ci && npm run build && npm test)
(cd failure-frontend && npm ci && npm run build)
```

构建输出分别位于两个源码目录的 `dist/`，默认由 Git 忽略；生产安装脚本继续使用父目录的已验收构建产物。装配看板源码中的生产快照已清空，保留 Excel 上传和解析逻辑；`machine-watch-dist/data/machine_watch_data.json` 仍是为保持静态页面结构和 `/auth/system-health` 可用而保留的空数据快照。失效分析、追溯、装配和车辆的历史数据库、上传文件、业务清单等均不在仓库。
