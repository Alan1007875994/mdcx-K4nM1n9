# MDCx

![python](https://img.shields.io/badge/Python-3.13-3776AB.svg?style=flat&logo=python&logoColor=white)

## 上游项目

* [yoshiko2/Movie_Data_Capture](https://github.com/yoshiko2/Movie_Data_Capture): CLI 工具,
  开源版本现已不活跃, 新版本已闭源商业化.
* [moyy996/AVDC](https://github.com/moyy996/AVDC): 上述项目早期的一个 Fork, 使用 PyQt 实现了图形界面, 已停止维护
* @Hermit/MDCx: AVDC 的 Fork, 一度在 [anyabc/something](https://github.com/anyabc/something/releases) 分发源代码及可执行文件.
* 2023-11-3 @anyabc 因未知原因销号删库, 其分发的最后一个版本号为 20231014.
* [@sqzw-x/mdcx](https://github.com/sqzw-x/mdcx)当前暂时停止维护.
* [@Hazard804/mdcx](https://github.com/Hazard804/mdcx)：Fork 自 [@sqzw-x/mdcx](https://github.com/sqzw-x/mdcx)。
* 本仓库 [@Marcus515J/mdcx](https://github.com/Marcus515J/mdcx)：Fork 自 [@Hazard804/mdcx](https://github.com/Hazard804/mdcx)，继续进行维护及优化。

向相关开发者表示敬意.

## 功能与版本说明

正式功能说明：[文件清理](docs/garbage-cleanup.md)、[云端翻译与本机备用](docs/llm-refusal.md)、[本机模型管理](docs/local-models.md)。

* **翻译引擎**：使用大模型 API（支持兼容云端主接口与本机备用模型、独立主备温度），旧版翻译引擎已移除（旧配置自动平滑迁移）。
* **扩展资源**：演员头像包、字幕包、水印包及演员资料库等第三方资源统一托管于私有资源仓库，需使用有访问权限的账号登录下载。

> 一般情况无需自行构建，请到[本仓库 Releases](https://github.com/Marcus515J/mdcx/releases) 下载发布版本。
> 当前提供 Apple 芯片 Mac（macOS aarch64）和 Windows x86_64 安装包；上游仓库的版本与本仓库独立发布。

正式版沿用本仓库的 `v主版本.次版本.修订号`，当前为 **v1.3.2**，延续 `v1.0.1 → v1.0.2 → v1.1.0 → v1.1.1 → v1.2.0 → v1.3.0 → v1.3.1`。
软件显示、更新比较、发布标签和成品名称统一使用这套编号；系统文件属性使用对应的数字版本 `1.3.2`。
曾发布的 `220261012` 日期编号版已被替代，下载最新版并沿用原配置即可；旧版更新检查不能解析新标签，需要手动替换一次。

本版汇集 v1.3.1 之后的网络请求失败容错、阶段后缀识别与演员对照表修正；翻译使用大模型 API。日常推荐配置 2.15；配置文件由使用者单独保留，不入库、不随发布包分发。完整变更见 [发布说明](changelog.md)。

### 开发与构建环境

当前主分支要求 Python 3.13.4 或更新版本，依赖和验证方法见 [开发说明](CONTRIBUTING.md)。自动发布使用 Python 3.13，构建目标为 macOS aarch64 和 Windows x86_64。

旧 `Build and Release v1` 工作流已移除，历史仍可在 Git 中查阅。当前构建与版本发布流程见 [开发说明](CONTRIBUTING.md)，安装包以本仓库 Releases 实际提供的文件为准。

## 授权许可

本插件项目在 GPLv3 许可授权下发行。此外，如果使用本项目表明还额外接受以下条款：

* 本项目仅供学习以及技术交流使用
* 请勿在公共社交平台上宣传此项目
* 使用本软件时请遵守当地法律法规
* 法律及使用后果由使用者自己承担
* 禁止将本软件用于任何的商业用途
