# 第三方组件与资料

本项目对原创代码的非商用许可不替代第三方权利人的许可。

- `static/vendor/cytoscape.min.js`：Cytoscape.js，MIT；文件头部保留原始版权及许可声明。
- FastAPI、Uvicorn、Requests、NetworkX、bibtexparser、scikit-learn 等运行依赖通过包管理器安装，各自保留原许可。
- pypdf：BSD-3-Clause，用于读取 PDF 文字、页数和书目属性；保留其版权、许可条件与免责声明。
- pypdfium2：Apache-2.0 / BSD-3-Clause；其使用的 PDFium 采用 BSD 风格许可，附带组件另有各自许可。通过包管理器安装时保留包内许可；如分发二进制，应同时提供 PDFium 及其构建所含组件的许可文件。用于原文页面渲染。
- Pillow：采用其随包提供的 MIT-CMU 许可，用于生成原文页面图像；分发时保留相关版权与许可。
- FontTools：MIT，用于读取论文 PDF 中的嵌入字体编码；分发时保留相关版权与许可。
- ReportLab：BSD 风格许可，仅用于开发测试生成合成 PDF，不是软件运行所需的依赖；其具体条款以安装包附带的 LICENSE 为准。
- 演示论文、图表、书目和学术接口数据不因本仓库许可而改变其原有权利或许可。仓库不收录论文 PDF、原图或个人数据库。


## 浏览器采集

软件使用 Playwright 管理独立浏览器，通过论文页面的公开书目信息和原文链接获取附件，不要求安装浏览器扩展。本项目不打包 Zotero Connector 源码、浏览器个人配置、登录数据或用户附件；Playwright 及浏览器遵循各自许可。
