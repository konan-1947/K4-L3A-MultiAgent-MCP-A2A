# L3A Architecture Record

Tài liệu này mô tả các quyết định quan sát được của workflow. Trace chỉ ghi
event, decision code và evidence reference; không ghi prompt hay chain-of-thought.

## 1. System overview

Mỗi case được xử lý độc lập và mọi MCP request đều mang theo cùng `case_id`.
Coordinator đọc `inputs/<case_id>.json`, giao việc cho các specialist, gom
evidence, rồi verifier tạo output theo `day09-l3a-output-v2`.

```text
Input
  -> Coordinator
      -> Order/item agent  -> get_order, get_order_items
      -> Payment agent     -> get_order_payments, get_payment_timeline,
                              get_refund_timeline (khi liên quan)
      -> Shipment agent    -> get_shipment_summary
      -> Seller lookup     -> get_sellers (khi cần party seller)
      -> Policy agent      -> get_policy
  -> Verifier
  -> Output + trace
```

`get_customer_history` và `get_product_context` được discovery để bảo đảm tool
profile, nhưng L3A output không có customer/product fields. `get_sellers` chỉ
được gọi khi cần xác minh party seller; các evidence refs trong output được lọc
theo domain liên quan đến từng primary issue.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case JSON | Xác định order, policy và claim topic; điều phối specialist | Giao scope theo `case_id` |
| Order/item | Order ID | Xác minh order status, item IDs, seller IDs | Order/item evidence cho payment và verifier |
| Payment | Order ID, claims | Đối soát payment rows, captured events và refund lifecycle | Payment evidence và payment handoff |
| Shipment | Order ID, item scope | Xác định delivered timeline, shipping limit và actor gây trễ | Shipment evidence cho verifier |
| Policy | Policy version | Lấy case status, action, responsible party và refund amount | Policy decision cho verifier |
| Verifier | Specialist results | Kiểm tra evidence refs, entity scope, claims, money và output contract | Final output và `verification_completed` |

Chỉ Order/item gọi `get_order` và `get_order_items`; Payment gọi ba payment
tools; Shipment gọi `get_shipment_summary`; Policy gọi `get_policy`. Coordinator
không tự tạo evidence. Gateway validate mọi MCP envelope trước khi trace hoặc
đưa evidence vào output.

## 3. A2A protocol

Handoff được biểu diễn bằng trace event với envelope quan sát được:

```text
{case_id, actor, target, decision_code, evidence_refs?}
```

`case_id` là correlation key bắt buộc. Luồng hiện tại là một chiều:
Coordinator → Order → Payment → Shipment → Policy → Verifier; không có vòng lặp.
Tool calls được thực hiện tuần tự trong một case để giữ thứ tự trace và tránh
đồng thời ghi trên cùng MCP session. CLI giữ session theo batch 5 case và có
thể mở lại batch tối đa bốn lần khi transport bị ngắt; trace partial của case
thất bại được rollback trước khi chạy lại. Bản thân tool call không
được retry âm thầm vì mọi call có thể bị audit. Lỗi được chuyển thành
`tool_unavailable` và không được thay bằng dữ liệu phỏng đoán.

## 4. Evidence lifecycle

1. Gateway gửi `case_id` cùng arguments tới MCP.
2. Gateway validate `schema_version`, `evidence_ref`, hash, domain và data.
3. Specialist giữ nguyên `evidence_ref` do server cấp; không sửa hoặc tạo ref.
4. Sau khi sử dụng, workflow emit `tool_result_consumed` với tool và ref.
5. Verifier hợp nhất refs duy nhất, lọc theo domain claim, rồi đưa vào claim
   assessments và output.
6. Case chỉ dùng refs của chính case đó; submission validator kiểm tra scope,
   team và run khi nộp.

Nếu tool không có dữ liệu, workflow ghi `handoff` với decision code
`tool_unavailable`, thêm conflict `tool_unavailable_no_guess`, và giảm
confidence. Không thêm evidence ref giả.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/transport error | Reconnect batch tối đa 2 lần; không retry âm thầm tool | Giữ thiếu evidence, chuyển verifier xử lý | `handoff/tool_unavailable` |
| Not found/tool error | No | Không đoán; status `needs_investigation` nếu không đủ evidence | `handoff/tool_unavailable` |
| Source conflict | No | Ưu tiên timeline/policy authoritative source và ghi conflict | `policy_decided` + conflict |
| Invalid specialist result | No | Verifier từ chối result và giữ output tối thiểu theo evidence thật | `verification_completed` |

Refund timeline được truy vấn cho order/refund issue để đối soát claim hoàn tiền;
lỗi “không có refund event” chỉ được ghi nhận là tool unavailable tùy chọn,
không bị biến thành refund hay data conflict. Seller lookup cũng là call tùy
chọn và chỉ phục vụ trách nhiệm seller.

## 6. Verification invariants

Trước finalize, verifier kiểm tra:

- output dùng đúng schema và đúng `case_id`;
- tất cả evidence refs có format hợp lệ, duy nhất và đến từ MCP call của case;
- order/item/seller/payment/shipment IDs lấy từ evidence, không tự đặt;
- mỗi claim assessment trỏ tới evidence refs liên quan;
- `recommended_refund_brl` bằng tổng `refund_lines.amount_brl`;
- currency luôn là BRL và tiền không âm;
- case status, policy action, responsible party và refund amount không mâu thuẫn;
- confidence nằm trong [0, 1];
- không có duplicate resolution actions hoặc conflict vượt giới hạn schema.

## 7. Reproducibility

- Python `>=3.11`, dependency ranges được pin trong `pyproject.toml`.
- Chạy bằng `.venv`: `source .venv/bin/activate`.
- Lệnh chính: `day09 validate-inputs`, `day09 mcp-tools`, `day09 run`,
  `day09 validate`, `day09 package --output dist/submission.zip`.
- Workflow deterministic, không dùng random seed hay model ngoài.
- MCP calls tuần tự trên mỗi case; không ghi API key vào source, output, trace
  hoặc package.
