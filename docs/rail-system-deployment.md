# 轨道客室系统报告服务部署

更新日期：2026-10-10。Presenton 提供 DOCX、PPTX 和 Markdown 报告生成，调用同一套本地 Qwen3-VL；不自行占用 GPU 或启动第二套大模型。完整系统使用根目录 `deploy.sh` 和 [Ubuntu 手册](../../../DEPLOY_UBUNTU.md)，编排为 `../../deployment/compose.yaml`。

## 构建与模型

当前镜像为 `rail-presenton:2026.10.10`，从本项目 Dockerfile 和本地源码构建。Python、Node、Chromium、字体、OCR 程序和项目内 presentation-export 属于程序依赖；构建可联网获取依赖，但不下载语言模型或 OCR 权重，不复制宿主虚拟环境。

`models/presenton_models/tesseract/eng.traineddata` 和 `osd.traineddata` 必须在部署前准备。Dockerfile 仅安装 Tesseract 程序及共享库；Compose 将两份权重只读挂载到系统 tessdata。整个 `models/` 只读挂载，运行时 HF/Transformers 离线；缺失权重或校验失败时停止，不回退下载。

## 服务与数据

`assistant` / `all` 模式包含 Presenton 和 vLLM，Presenton 在 internal 网络访问 `http://vllm:8000/v1`，默认模型名 `qwen3-vl-8b-instruct`。业务用户通过平台访问报告，不直接访问 Presenton；上游云端提供商、联网检索、外部图片生成和遥测不用于本系统验收。

默认 lexical 图标检索，Mem0、联网图片生成和 web grounding 关闭。运行输出保存在命名卷，平台报告回流到 PostgreSQL/MinIO 的项目资产；数据库、对象、报告输出、密钥不进入 Git 或源码交付包。首次部署生成独立凭据，重复部署不重置用户数据。

## 验收与独立调试

本机已通过真实 Qwen3-VL 文字/图片、DOCX/PPTX/Markdown AI 报告及模板报告，OCR eng/osd 本地只读权重与识别检查通过。镜像 ID 和系统测试边界见 [验收记录](../../../DEPLOY_VALIDATION_20261010.json)。目标服务器、生产负载与三 GPU 并发仍需现场测试。

本次文档/远程同步前重新通过 `npm test` 6 项、`npm run check:presentation-export` 和整体部署脚本 62 项回归；没有重启模型服务或重新构建镜像，历史实机测试与本次源码检查分别记录。

`presenton.sh`、`docker-compose.yml` 和 host.docker.internal 覆盖配置是保留的子项目独立调试入口；不能与整体私密状态目录或业务卷混用。接口字段和输出限制见 [离线文件生成 API](offline-file-generation-api.md)。
