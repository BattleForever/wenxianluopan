<p align="center">
  <img src="docs/images/readme-hero.png" width="1280" alt="文献罗盘：双向引文探索、AI 双语速览与全文解读。工作数据保存在本机。">
</p>

<p align="center">
  <a href="#特色功能">特色功能</a> &nbsp; · &nbsp;
  <a href="#快速开始">快速开始</a> &nbsp; · &nbsp;
  <a href="docs/USER_GUIDE.md">使用说明书</a> &nbsp; · &nbsp;
  <a href="https://github.com/BattleForever/wenxianluopan/releases/latest">下载软件</a>
</p>

**文献罗盘是一款 AI 辅助的科研文献调研与阅读工具。** 从已知论文出发，双向查找研究基础与后续进展；在引文图谱中筛选候选，用 AI 翻译、概括和全文解读理解研究内容，再将论文与阅读笔记按专题保存。

适用于开题调研、了解新的研究方向、比较相关工作和准备综述。

> **文献记录、分类、阅读笔记、AI 结果和连接配置均保存在本机，可自行备份与迁移。** 使用在线检索、AI 阅读或 Zotero 同步时，会向相应服务发送该操作所需的信息。

![文献探索工作界面](docs/images/workspace.png)

## 特色功能

- **双向引文探索**：沿参考文献追溯研究基础，沿施引文献跟踪后续工作。结合引文关联与主题匹配推荐阅读起点，查看分组依据，再按需扩大探索范围；已发现的候选完整保留。
- **AI 双语速览**：保留原文题名与摘要，补充中文翻译和概括，整理研究问题、方法、结果与贡献；通过原文引用核对要点，辅助判断论文是否值得精读。
- **AI 全文解读**：围绕文献原文逐节梳理背景、方法与研究结果，结合正文和图注解释主要图表，归纳结论与局限；按页码返回原文核对，并导出阅读成果。
- **资料补全与专题整理**：按需补充书目、期刊指标和中文内容；文献库积累研究资料，阅读清单管理本轮待读论文，分别支持多级分类、拖拽整理和回收站。

## 快速开始

先安装 **Python 3.11 或更新版本**，再从[最新发布](https://github.com/BattleForever/wenxianluopan/releases/latest)下载源码包并解压。

1. **启动软件**：Windows 下双击程序文件夹中的 `start.bat`。首次启动会安装依赖，请保持联网；就绪后自动打开页面。
2. **配置 AI**：进入 **数据与设置 → AI 模型 → 新建连接**，填写服务地址与 API 密钥，获取或手动填写模型，保存后测试连接。
3. **导入起点**：拖入 PDF、BibTeX、RIS 或 JSONL 文件，也可粘贴 DOI，或从 Zotero 勾选文献与集合导入。
4. **探索与阅读**：选择种子论文开始拓展。先查看“入门重点”，按需切换分组与筛选，再打开 AI 速览或全文解读。
5. **保留成果**：把论文存入文献库或阅读清单，选择分类、记录备注，或导出文献与阅读成果。

首次使用 AI 前需要提供自己的模型服务。单纯浏览、整理已有文献不需要 AI 密钥。详细操作见[使用说明书](docs/USER_GUIDE.md)。

<details>
<summary>终端启动（macOS、Linux 或手动安装）</summary>

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate
pip install -r requirements.txt
python launcher.py
```

启动器会显示实际地址，并在服务就绪后打开浏览器。Windows 提供系统文件夹选择与到期内容转入系统回收站等集成；其他系统的操作差异见说明书。

</details>

### 重新打开与退出

双击 `create-shortcut.bat` 创建带品牌图标的桌面快捷方式。以后双击桌面“文献罗盘”即可启动或重新打开；没有快捷方式时，双击程序文件夹中的 `start.bat`。

关闭网页后后台仍会运行，重新打开会复用已有服务。需要完全关闭时，点击页面右上角 **退出** 并确认。移动程序文件夹后，请重新创建快捷方式。

## 按自己的方式阅读与整理

**模型连接**支持 DeepSeek、OpenAI、通义千问、Kimi、智谱等服务预设，以及自定义兼容 OpenAI 接口的服务。可管理多组连接、备用密钥与模型列表，设置请求并发数；任务进度统一在可拖动的“小书灵”中查看。

**文献原文**可从本机、Zotero 附件或可访问的公开来源获取；需要网页访问时，可使用软件自带的独立采集浏览器，无需安装浏览器扩展。原文可保存至 Zotero、指定目录，或每次选择位置，取得后用于全文解读。

**文献库**保存长期研究资料；**阅读清单**管理本轮准备阅读的论文与备注。两者分别设置分类，同一篇论文可归入多个专题。工作空间提供项目目录、成果导出、备份与迁移入口。

![文献库与专题文件夹](docs/images/folders.png)

## 常用入口

- [引文探索、分组与图谱操作](docs/USER_GUIDE.md#explore)
- [AI 双语速览](docs/USER_GUIDE.md#ai-reading) · [AI 全文解读](docs/USER_GUIDE.md#full-reading)
- [获取与管理原文](docs/USER_GUIDE.md#originals) · [保存到 Zotero](docs/USER_GUIDE.md#zotero)
- [资料补全与本地期刊指标库](docs/USER_GUIDE.md#completion)
- [文件夹与回收站](docs/USER_GUIDE.md#organize) · [备份与迁移](docs/USER_GUIDE.md#backup)
- [连接配置](docs/USER_GUIDE.md#connections) · [任务与常见问题](docs/USER_GUIDE.md#help)

## 许可

**本项目源码公开**，采用 [PolyForm Noncommercial License 1.0.0](LICENSE.md)。允许在许可范围内使用、修改与再分发；传播时保留许可证及 [NOTICE](NOTICE) 中的版权与来源声明。**超出公开非商业许可范围的商业使用，必须先联系作者并取得书面商业授权，费用或分成另行约定。** 联系方式与具体说明见[许可与商业授权](LICENSING.md)，第三方组件及论文资料另遵循各自许可。
