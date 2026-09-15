# 质量门户前端源码

这是本机 `/Users/dutao/Desktop/project1/quality-platform` 中发现的 Vite/React 源码，包含门户入口、权限管理和装配工单 SQ 看板源码及测试。未复制 `node_modules`、`dist`、Playwright 截图和 TypeScript 构建缓存。

```bash
npm ci
npm run build
npm test
```

构建输出在本目录的 `dist/`（已被 Git 忽略）。生产安装脚本使用父目录已有的 `portal-dist`；若要重新发布，应先审查构建输出并替换相应静态目录。装配看板源码保留上传 Excel 的处理逻辑，但默认数据已清空，不含本机发现的生产快照。
