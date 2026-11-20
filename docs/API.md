# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。`/work/8`为撤销冲减页。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

## 撤销冲减

仅对已通过复核的批次开放；冲减以追加分录记录，不改写原批次、客户余额与历史凭证。

`POST /api/withdrawal`：`run_id`（缺省最新批次）、`withdrawal_id`（撤销决定编号）、`payments`（数组，缺省时按已复核记录的渠道报送现金派生，显式空数组表示确认无付款）、可选`reason`。每个付款条目为`record`、可选`security`、`status`（已付/在途/未付）、`amount`、可选`voucher`/`paid_at`。返回每户应付、已付、在途、未付、超额、冲减、转应收、终止支付与处理说明，以及`totals`、`checks`、`blockers`、`open_items`、`can_close`、`status`。

`GET /api/withdrawal/<withdrawal_id>?run_id=`：读取决定。`GET /api/withdrawals?run_id=`：批次下的撤销决定。

`POST /api/withdrawal/events`：字段同上，并带`event`（`record`、`status`（已付/未付）、`amount`、可选`voucher`/`paid_at`/`note`）。晚到付款或止付确认只追加事件并重算派生视图，`confirmed_rows`/`confirmed_totals`保持决策时点原值。

`POST /api/withdrawal/close`：`run_id`、`withdrawal_id`、`revision`。存在在途或无法解释的付款时返回409 `NOT_CLOSABLE`；已结批后重复调用返回原结果（`close_reused`）。

`GET /api/withdrawal/evidence.csv?run_id=&withdrawal_id=`：撤销证据导出，含合计、逐户冲减、非本批付款、异常与事件区段。

幂等：`run_id + withdrawal_id`为撤销决定的幂等键，同键同内容重跑返回原结果且`reused`为true，不重复冲减；同键不同内容返回409；同一批次只允许一个撤销决定，后续到账走事件接口。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。
