# 第三方组件与资料

本项目对原创代码的非商用许可不替代第三方权利人的许可。

- `static/vendor/cytoscape.min.js`：Cytoscape.js，MIT；文件头部保留原始版权及许可声明。
- FastAPI、Uvicorn、Requests、NetworkX、bibtexparser、scikit-learn 等运行依赖通过包管理器安装，各自保留原许可。
- PyMuPDF：由用户通过包管理器安装，遵循其 AGPL / 商业双重许可安排；本仓库不包含该库的二进制或源码。使用及分发时应同时遵守该依赖自身许可。
- 演示论文、图表、书目和学术接口数据不因本仓库许可而改变其原有权利或许可。仓库不收录论文 PDF、原图或个人数据库。


## 浏览器采集

软件使用 Playwright 管理独立浏览器，通过论文页面的公开书目信息和原文链接获取附件，不要求安装浏览器扩展。本项目不打包 Zotero Connector 源码、浏览器个人配置、登录数据或用户附件；Playwright 及浏览器遵循各自许可。
