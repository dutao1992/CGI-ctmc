# 失效分析前端源码

这是本机 `/Users/dutao/Desktop/project1/failure-analysis/frontend` 中发现的 Vite/React 源码。API 通过 Vite 开发代理访问本地 `127.0.0.1:8010`，生产入口由父目录的 Nginx 配置提供。

```bash
npm ci
npm run build
```

构建输出在本目录的 `dist/`（已被 Git 忽略）。生产安装脚本使用父目录已有的 `failure-dist`；重新发布前应先审查构建结果。
