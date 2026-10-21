# 文献罗盘

<img src="static/brand/wenxianluopan-icon-64.png" width="64" alt="文献罗盘图标">

**AI 驱动的科研文献调研与速览平台**

从一篇已知论文出发，沿参考文献追溯研究基础，沿施引文献追踪后续进展；通过 AI 翻译、概括与全文导读，形成带有来源依据的阅读清单。

面向研究生开题、课题组新成员入门、研究方向拓展和综述准备。

![文献罗盘实际界面](docs/images/workspace.png)

## 功能

- **双向引文拓展**：以一篇或多篇论文为种子，逐轮拓展参考与施引文献；图谱和列表保留发现方向、轮次、路径与来源，可控制采样范围。节点采用疏朗布局，可按被引数、期刊影响因子或年份渐变着色，并联动列表排序；缺失指标显示为灰色。
- **候选文献筛选**：按研究问题进行文本相关性排序，结合年份、被引数、已有期刊指标及引用网络线索比较候选论文。
- **AI 双语速览**：翻译题名，概括摘要，归纳研究问题、方法、结果与贡献；点击引用返回原文核对。
- **可选 AI 全文解读**：点击图谱节点，在详情中选择「AI 全文解读」。遵循 zotero-pdf 的附件定位、文本提取和中文导读流程，提供逐节解读、图表说明、结论与局限，并定位 PDF 原文页码。
- **阅读整理**：保存阅读备注、导出 CSV / RIS / GraphML；支持 Zotero 授权写回与状态反馈。

## 快速开始

需要 Python 3.11 或更高版本。Windows 可双击 `start.bat`；也可以：

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

浏览器打开 `http://127.0.0.1:8765`。通过 DOI、BibTeX、RIS、PDF 文件夹或 Zotero 导入文献，选择种子后开始拓展。

## 配置 AI

把 `ai_config.example.json` 复制为 `ai_config.local.json`，在自己电脑上填写模型密钥；该文件已被 Git 忽略。默认接入浙江大学模型网关。也可以通过 `PAPERNET_AI_CONFIG_FILE` 指定配置文件，通过 `PAPERNET_LLM_API_KEY` 提供密钥。

模型请求由用户点击触发。摘要速览发送题名和摘要；全文解读发送对应论文的 PDF 提取文本。离线模式不产生模型请求，已保存结果仍可查看。请仅处理自己有权使用和传输的文献。

## 使用全文解读

1. 在 Zotero 中为论文保存可读 PDF，或在平台导入 PDF 文件夹。
2. 点击图谱节点，选择 **AI 全文解读**，再点击 **生成全文解读**。
3. 通过「概览、逐节导读、图表说明、结论与局限」阅读结果；点击依据按钮核对 PDF 页码和高亮文本。
4. 可导出 Markdown 导读。再次打开时读取已保存的结果，PDF 或题名变化时显示历史结果提示。

Zotero 附件定位只查询临时数据库副本。读取全文不修改 Zotero 原库；同步写回是独立的授权操作。全文解读支持可提取文本的 PDF，当前页面上限 80 页；扫描件需要先完成 OCR。AI 输出供辅助阅读，概括和判断应与原文核对。

## 演示案例

Gao et al., *High frame rate ultrasonic flowmeter based on PMUT array with bidirectional acoustic beams*, Microsystems & Nanoengineering (2025). DOI: [10.1038/s41378-025-01096-x](https://doi.org/10.1038/s41378-025-01096-x)。

该论文在原 Zotero 条目中也曾使用 “Development of a high frame rate bidirectional ultrasonic flowmeter based on beamforming using pMUT arrays” 这一题名；通过 DOI 确认身份。

仓库不包含个人文献库、论文 PDF、运行数据库、模型密钥或个人申报信息。

## 许可

本项目原创代码采用 **PolyForm Noncommercial License 1.0.0**，源码公开，**仅限非商业用途**。未经权利人另行书面许可，不得将本项目用于销售、付费服务、商业托管或其他许可范围之外的商业活动。使用、修改和再分发须遵守 [LICENSE.md](LICENSE.md)，保留署名与许可通知。

这是一种限制商业用途的源码公开许可，不宣称符合 OSI 开源定义。第三方组件及论文资料保留各自许可，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
