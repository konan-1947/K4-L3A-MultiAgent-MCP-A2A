# Tài liệu kiến trúc L3A

## 1. Tổng quan hệ thống

Workflow nhận một case, phân công các specialist thu thập evidence từ MCP theo chủ đề claim, xác minh phạm vi/domain, rồi tạo output và trace. Nếu tool lỗi hoặc chưa đủ dữ liệu để kết luận, case được giữ ở trạng thái cần điều tra; workflow không suy đoán từ lời khách hàng.

```text
Input -> Coordinator -> Order / Item / Payment / Shipment / Seller / Policy agents
                |                         |                  |
                +---------------------- MCP ----------------+
                +-> Verifier -> Output
                +--------------> Trace
```

## 2. Phân công vai trò agent

| Actor | Trách nhiệm | Tool được dùng |
| --- | --- | --- |
| Coordinator | Kiểm tra case, chọn các tool cần tra cứu theo topic claim và giao việc | Discovery MCP |
| Order agent | Xác minh đơn hàng | `get_order` |
| Item agent | Xác minh item và ngữ cảnh sản phẩm | `get_order_items`, `get_product_context` |
| Payment agent | Xác minh payment và refund lifecycle | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| Shipment agent | Xác minh các mốc và sự kiện giao hàng | `get_shipment_summary` |
| Seller agent | Xác minh seller liên quan đến item trong đơn | `get_sellers` |
| Policy agent | Lấy policy đúng phiên bản; không khuyến nghị refund nếu chưa xác minh eligibility | `get_policy` |
| Verifier | Kiểm tra domain, phạm vi case/order, evidence coverage và contract | Không gọi tool |

Tool được chọn theo `topic` của claim. Các claim không nhận diện được vẫn được chuyển verifier ở trạng thái chưa đủ evidence; workflow không đoán tool.

## 3. Giao thức A2A

Workflow hiện chạy tuần tự trong cùng tiến trình; chưa triển khai giao thức A2A qua mạng. Các lần bàn giao quan sát được thể hiện bằng sự kiện giao việc trong trace và được liên kết bằng `case_id`.

## 4. Vòng đời evidence

Mỗi MCP call truyền `case_id` của case hiện tại; các tool liên quan đơn hàng cũng nhận mã đơn khách hàng khai báo. Gateway xác thực response contract. Workflow kiểm tra domain mong đợi và, với order data nếu response có mã đơn, kiểm tra mã đó khớp. `evidence_ref` do server cấp được giữ nguyên, ghi vào trace và output; evidence không được chia sẻ giữa các case.

## 5. Chính sách xử lý lỗi

| Lỗi | Thử lại | Phương án dự phòng | Cách xử lý |
| --- | --- | --- | --- |
| MCP timeout hoặc tool báo lỗi | Không tự động thử lại | Không có | Ghi handoff lỗi, tiếp tục các tool độc lập còn lại và giữ case cần điều tra |
| Thiếu mã đơn khách hàng khai báo | Không | Không có | Dừng case với lỗi xác thực |
| Evidence sai domain hoặc mã đơn | Không | Không có | Từ chối evidence và giữ case cần điều tra |
| Thiếu evidence hoặc chưa xác minh được semantics | Không áp dụng | Không suy đoán | Claim `insufficient_evidence`; không đề xuất refund |

## 6. Các điều kiện bắt buộc khi xác minh

- Mọi output và trace event đều được kiểm tra theo JSON schema tương ứng.
- Giá trị `evidence_ref` do MCP trả về được giữ nguyên, không trùng lặp trong danh sách tham chiếu và chỉ gắn với case tương ứng.
- Không dùng evidence đơn hàng để tự suy ra sự kiện thanh toán, vận chuyển, trách nhiệm hoặc quyền hoàn tiền.
- Không đề xuất số tiền hoàn hoặc hành động xử lý nếu thiếu evidence hỗ trợ.
- Bộ phân loại semantics/root cause/refund chưa hoàn thiện; đến khi response MCP thực tế được xác minh, kết luận được giữ ở trạng thái `needs_investigation`.

## 7. Khả năng tái lập

Chạy `day09 mcp-tools`, `day09 run` và `day09 validate` từ thư mục gốc repo sau khi cấu hình `.env`. `day09 mcp-tools` hiển thị tên, mô tả và schema tham số của các tool. Không ghi Team API Key vào trace hoặc tài liệu kiến trúc.
