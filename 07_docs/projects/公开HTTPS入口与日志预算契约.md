# 公开HTTPS入口与日志预算契约

## 用户范围

用户没有域名，使用现有公网IPv4申请受信任的IP证书。所有访客均可浏览、修改和保存，
不新增登录或管理员限制；原输入验证、版本比较、CAS、来源证据和业务发布授权保持有效。
本项目不采集行情，不替换原来源，不恢复已弃用数据库；原独立更新任务和业务日历不变。

## 独立入口

`09_deploy/https_entrypoint/` 只管理入口、证书和运行维护回执。网关使用固定官方Nginx镜像，
非root、只读根、无capabilities、独立bridge，128MiB内存、无额外swap；仅配置、证书、
HTTP-01挑战目录RO挂载，临时写入落32MiB tmpfs。无Docker socket、业务数据或应用secret挂载。

HTTPS根路径代理原主站；`/usda/`、`/oil-world/`保持原原生路径；广西和云南分别使用
`/guangxi-weather/`、`/yunnan-weather/`，代理剥离前缀，保留原Streamlit相对资源与WebSocket。
上传、下载、查询及保存仍经原应用。HTTP80根路径转到HTTPS广西天气以保留原80入口语义；
其余HTTP路径转到相同HTTPS路径，HTTP-01验证路径单独提供静态挑战。不得给业务响应设小型上传上限。

原后端端口8501、8080、8081、8856、8858仅在公网入站收敛。局域bridge、loopback、SSH、
443、80和其他端口不改。独立`MARKET_HTTPS_ONLY`链通过DOCKER-USER按公网接口和原始目标
端口拒绝TCP ORIGINAL连接，不能清空或覆盖其他规则；未知已有同名规则停止。公网IPv6未验收时
不得宣称IPv6收敛。必须先在真实外部浏览器验证受信任HTTPS、五个页面、资源、WebSocket、
下载及原保存功能，再启用端口收敛及其15分钟复核timer；内部TLS健康不替代这次外部验收。

## 证书状态

官方Certbot工具镜像仅挂载独立ACME账户、工作、日志及挑战目录；无业务目录和Docker socket。
使用webroot、shortlived IP证书，先独立staging账户验证，再申请可信生产证书。无域名、无邮箱
时非交互注册；账户私钥与证书私钥只在受保护主机目录保留，不进入Git、镜像、网页或操作输出。

固定镜像的原始registry index、linux/amd64 manifest、config与解压层SHA传输证据单独封存。
Nginx 1.28.2-alpine Image ID为`sha256:43a775095fde460e747cc4799d2060c1953d588f6b896443051acccab1bb355f`；
Certbot v5.4.0为`sha256:f0cfa0056d76325c6a2d3f4dc4dc41449763a9ee1000ed11caf8dc180450144d`。
不在服务器临时安装Certbot或其他依赖；跨环境导入必须逐层验证解压SHA和最终Image ID。

每6小时检查续期，失败15分钟后重试，一小时最多3次。使用系统CA验证信任链、IP SAN、
证书/私钥配对和至少36小时有效期；通过后生成独立证书版本，原子切换指针，Nginx检查后HUP。
失败保留原证书，HUP校验失败恢复旧指针；staging或dry-run证书永不发布。不关闭TLS校验，
不使用自签证书冒充正式HTTPS，不让Certbot执行任意部署hook。

## 日志与回执

主站、USDA、Oil World、两个天气读者及网关分别使用json-file，max-size=20m、max-file=5，
合计约600MiB。采集子容器同样声明轮转。配置对新容器生效，不能手工截断运行中的Docker日志。
替换前封存原日志并校验SHA，现有应用使用原exact image和原数据挂载逐项重建/恢复；
主站仍执行原签名、同镜像候选、生产授权与回滚流程。其他模块不因配置刷新重新构建或采集。
本项目不清理历史journal、业务日志、旧候选、镜像、源码或恢复资产。

主机配置固定Commit/Tree/Origin及实际主站RO数据挂载。只新增
`update_status/https_entrypoint.json`维护元数据，记录真实核验时间、证书截止日、六个容器
日志预算和旧端口隔离结果。运行监控页只读此文件，超时、续期失败、日志预算不符或端口核验
失败分别提醒；不能把它当业务数据已更新。主机原子写入、锁和受保护源码校验保持有效。
原84项业务依赖、13个更新任务及已暂停的来源清单不因此扩展。

## 验收和恢复

发布前取得完整exact CI及所需真实发布路径；使用localhost-only独立候选核对配置、HTTP、
真实页面及WebSocket，staging再验证HTTP-01和续期dry-run。所有CA与系统配置操作留下回执。
切换前保存五个应用的实例、exact image、Compose/env/unit、挂载和原日志，以及新旧网关配置。
释放原广西HTTP80绑定仅刷新该读者，保留8856、原只读快照和更新器；网站短暂重连应明确记录。

只有可信HTTPS外部验收通过才收敛旧端口。失败保留旧入口；如已切换，恢复原读者unit/配置、
原exact应用镜像和fresh grant，撤回仅本项目拥有的DOCKER-USER跳转及规则，不覆盖其他链。
恢复不删除任何业务历史、新证书、账户或维护证据；老读者忽略独立HTTPS回执，不需反向迁移。
