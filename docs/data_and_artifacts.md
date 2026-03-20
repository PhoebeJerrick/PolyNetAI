# 数据与产物：版本库策略

本仓库**默认不把原始/处理后的 Excel 数据**和**运行产物**纳入 Git，原因如下。

## 为什么不把数据放进 Git？

| 类型 | 原因 |
|------|------|
| **体积** | `xlsx` 等易膨胀，拖慢 clone/fetch，且 Git 对大二进制 diff 不友好。 |
| **隐私与合规** | 交易记录、账户相关字段可能涉及个人或平台条款，公开仓库风险高。 |
| **可复现性** | 数据常更新；版本库里放一份“快照”容易与各人本机不一致，反而误导。 |
| **协作** | 每人数据源路径、更新频率不同，用文档约定「放哪、叫什么」比强行提交更稳。 |

## 本地应如何放置数据？

1. 在仓库中保留空目录占位：`data/raw/.gitkeep`、`data/processed/.gitkeep`（已纳入版本库）。
2. 将原始表放到 **`data/raw/`**，例如：
   - `data/raw/polymarket_tracker_collection.xlsx`
3. 分析脚本默认输出到 **`data/processed/`**（与 `README.md`、脚本 `--help` 一致）。

若你从未拉取过数据，从可信来源取得文件后按上述路径放置即可运行 `README.md` 中的命令。

## 产物目录 `artifacts/`

回放、live、sweep、optimize、dashboard 等输出默认写在 **`artifacts/`** 下，已在 `.gitignore` 中忽略。

- 需要分享某次实验结果时：打包该子目录、或导出关键 `csv`/`yaml` 到 `docs/`（小文本）再提交。
- 不要把整棵 `artifacts/` 长期堆在公开仓库里。

## 若必须「在 Git 里」存大文件

可选方案（按常见程度）：

1. **Git LFS**（适合必须版本化的中等体积二进制）：在仓库根配置 `.gitattributes`，并对 LFS 跟踪的路径**不要**再用 `.gitignore` 挡住。需团队都安装 Git LFS。
2. **外部存储**：对象存储 / 网盘 / 团队共享盘，仓库里只放链接与校验和（如 SHA256）。
3. **发布附件**：GitHub Releases 上传压缩包，代码仓库保持轻量。

## 已从 Git 跟踪中移除的数据文件

历史上若曾把 `data/raw/*.xlsx`、`data/processed/*.xlsx` 提交过，后续会通过 `git rm --cached` 停止跟踪；**你本机文件仍会保留**，只是不再进入后续 commit。协作者需自备数据文件。

## 检查清单（提交前）

- [ ] 未提交 `.env`、私钥、含口令的 yaml
- [ ] 未误加 `data/raw/`、`data/processed/` 下的大表（除非刻意用 LFS 等方案）
- [ ] 未提交整目录 `artifacts/` 或本机一次性输出目录
