# Rail 受控模板内容规划

本轮与主平台的背景、设计输入暂存和提示词优化合并交付。此仓库仅负责独立 AI 内容规划，主平台负责上传模板、版本快照、用户资产权限和原生 Office 渲染。

新增 `models/template_report_plan.py`、`services/template_report_plan_service.py` 和 `/api/v1/generate-file/template-plan`。保留旧文件生成接口。长章节分段规划，代表预览与源图片位置分离；章节顺序、图片位置和原文数字保留必须通过验证。AI 不输出模板 XML/脚本或任意图片链接。临时预览在计划请求结束后删除。

本地测试包含 Pydantic/边界/模型协议单元测试、真实 Qwen3-VL 推理，以及主平台浏览器上传模板后 Word/PPT 两种模式的生成、进度和下载。独立 Docker 镜像单独构建与验证，不把模型依赖并入平台镜像。完整本轮结果以主平台 `docs/rail-platform/validation/REPORT_TEMPLATES_20261027.json` 为准；源码变更及本地验收不代表服务器已更新。

AI 输出的语义仍需人工核对；结构和数字约束不能证明所有事实都正确。第三方合同/专利/投标文本也需按正式适用条件确认，不能让模型补造主体、金额、合规性或承诺。
