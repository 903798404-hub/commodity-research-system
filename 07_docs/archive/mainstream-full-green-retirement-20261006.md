> ARCHIVED / NOT AUTHORITATIVE / DO NOT EXECUTE

# Full regression 过渡退出核验记录

2026-10-06 22:21（Asia/Shanghai）在线核对 main 为
`d0e3733100dd04ef209133b2b3212e6e0b889cf5`，与 fresh fetch/ls-remote 一致。
[GitHub CI run 37434592709](https://github.com/903798404-hub/commodity-research-system/actions/runs/37434592709)
为该提交的 PR run、attempt 1，所有实际 job success。
已下载封存结果中 base 4872 passed、candidate 4909 passed，两侧 failed/skipped 均为0，
required obligations 全绿；这满足旧规范规定的退出 debt ratchet 前提。

后续治理改动将 required full 改为单次 candidate ALL_GREEN，并保留平台/消费者验证、
精确 plan/run 身份及历史 paired 工具。该记录是前提观察，不宣称后续 PR 已通过或已接纳。
它不授予服务器部署、数据写入、日志配置或清理授权。
