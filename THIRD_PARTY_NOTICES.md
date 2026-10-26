# 第三方组件

Compute Studio 使用以下 Python 依赖，版本范围见 `requirements.txt`：Flask、Paramiko、keyring 和 Waitress。开发测试另使用 pytest。它们通过包管理器单独安装，许可信息以各项目及安装包内的原文为准。

网页随仓库分发以下渲染资源，正常运行无需从 CDN 加载：

| 组件 | 随附版本 | 用途 | 随附许可证 |
| --- | --- | --- | --- |
| [Marked](https://github.com/markedjs/marked) | 18.1.0 | Markdown 解析 | [MIT](static/vendor/marked/LICENSE) |
| [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.16 | HTML 净化 | [原始许可文本](static/vendor/dompurify/LICENSE) |
| [KaTeX](https://github.com/KaTeX/KaTeX) | 0.19.0 | 公式与字体渲染 | [MIT](static/vendor/katex/LICENSE) |

下载来源、包校验值和收录文件清单见 `static/vendor/notes-libraries.json`。请保留第三方许可证与版权声明；这些说明不替代或变更各组件的许可条款。

本项目暂未添加项目级许可证。界面截图使用模拟数据，Logo 为本项目的生成图像资源。
