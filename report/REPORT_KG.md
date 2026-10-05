# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Vũ Đức Minh  **MSSV:** 2A202602895  **Ngày:** 05/10/2026

Kết quả dùng cùng cấu hình `gpt-4o-mini`, `text-embedding-3-small`, `top_k=3`, `chunk_size=800`, 176 chunks và 20 bài tin. Hai lần chạy custom/hint dùng cùng bộ câu hỏi; benchmark custom cuối cùng là `ket_qua_benchmark_kg.txt`.

## 1. Chi phí (10 điểm)

### Indexing (one-off)

```text
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112     38.0
graph       196     91418     5397   0.00966    124.6
```

### Querying (mean per question)

```text
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.17      694       45   0.00012     1.31
graph       1.00   2.00     2540       89   0.00043     2.07
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | ---: | ---: | ---: |
| Indexing USD | 0.00112 | 0.00966 | 8.63× |
| Indexing giây | 38.0 | 124.6 | 3.28× |
| Mỗi câu: USD | 0.00012 | 0.00043 | 3.58× |
| Mỗi câu: giây | 1.31 | 2.07 | 1.58× |
| Mỗi câu: input tokens | 694 | 2540 | 3.66× |

Graph thêm 20 lần gọi trích xuất tin (196 so với 176 calls), lưu 5.397 output tokens khi index, và đưa nhiều dữ kiện graph vào prompt khi trả lời (2.540 so với 694 input tokens/câu). Chi phí index tăng khoảng 0.00854 USD; mỗi câu graph cũng đắt hơn khoảng 0.00031 USD theo số đã làm tròn trong benchmark, nên không có điểm hòa vốn nếu chỉ tính tiền API. Graph đáng dùng khi lợi ích trả lời câu xuyên luật–tin, điều kiện định lượng và tổng hợp nhiều vụ quan trọng hơn phần chi phí tăng thêm. Chi phí judge không nằm trong số liệu pipeline.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Cả hai lấy được trực tiếp định nghĩa từ corpus luật. |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Tên hai bị cáo và mức án nằm trong cùng bài tin truy xuất được. |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Graph nối mức án trong tin của Lê Minh Thành với Điều 251 và khoản 1. |
| Q4 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Trả lời hành vi và mức phạt tối đa cần đi từ người/vụ sang điều luật. |
| Q5 | cross-kb-multi-hop | 0.60 / 1 | 1.00 / 2 | Graph | Graph ghép lượng MDMA với đúng khoản 4, điểm b và ngưỡng 100 g. |
| Q6 | aggregation | 0.00 / 2 | 1.00 / 2 | Graph | Graph liệt kê đủ các nhóm vụ theo đáp án; Flat nêu các vụ mơ hồ và bỏ sót Viện Pháp y. |

Trên sáu câu, Graph tăng mean recall **0.57** và mean judge **0.83**. Hai câu hỏi đơn-hop hòa điểm; lợi thế tập trung ở cross-KB, multi-hop và aggregation.

## 3. Phân tích lỗi (20 điểm)

### Lỗi E3: Trùng vụ giữa các tài liệu

- **Hiện tượng:** một vụ Cái Quang Huy vận chuyển MDMA xuất hiện thành hai `Case` vì nội dung vụ được nhắc trong hai tài liệu. Hai bản ghi cùng có người, chất và lượng gần như nhau.
- **Bằng chứng:** truy vấn trên graph Neo4j sau khi nạp đủ corpus:

```cypher
MATCH (k:Case)-[i:INVOLVES]->(s:Substance {name:'MDMA'}),
      (p:Person {id:'cai quang huy'})-[:PARTICIPATED_IN]->(k)
WHERE i.raw_amount CONTAINS '9,6'
RETURN k.id, k.name, k.doc_id, k.source_title, p.name, s.name, i.raw_amount
ORDER BY k.doc_id;
```

```text
case_id: news-100260917203001265::case-1
doc_id:  news-100260917203001265
name:    Cái Quang Huy và Nguyễn Tiến Đạt vận chuyển trái phép chất ma túy
amount:  hơn 9,6kg MDMA

case_id: news-100260918080821054::case-2
doc_id:  news-100260918080821054
name:    Vụ án vận chuyển ma túy của Cái Quang Huy
amount:  9,6kg MDMA
```

- **Nguyên nhân:** `Case.id = doc_id::case-N` ưu tiên provenance và tránh gộp nhầm hai vụ có tên tương tự. Corpus có đoạn liên quan Cái Quang Huy ở cuối bài về kháng cáo của nhóm Lê Minh Thành; ontology hiện chưa có bước nhận diện cùng một sự kiện qua nhiều tài liệu.
- **Đề xuất sửa:** thêm `Event` canonical và cạnh `REPORTS`; chỉ gộp khi người, tội danh, chất/lượng và dấu hiệu thời gian/địa điểm cùng khớp, lưu mọi `source_doc_id`. Đánh đổi là thêm bước entity resolution và nguy cơ gộp nhầm nếu ngưỡng tương đồng đặt thấp.

### Lỗi E4: Điểm judge quá rộng so với đáp án aggregation

- **Hiện tượng:** Flat Q6 được keyword recall **0.00** nhưng LLM judge chấm **2**. Đọc câu trả lời cho thấy judge đã cho điểm tối đa dù danh sách vụ chưa đáp ứng đủ các vụ cụ thể trong gold.
- **Bằng chứng:** `ket_qua_benchmark_kg.txt`, `Q6 flat`: câu trả lời liệt kê “vụ việc của Đức”, “vụ việc của Thành”, “vụ việc của Đông”; không nêu `Cái Quang Huy`, tên đầy đủ `Lê Minh Thành` hay `Pháp y tâm thần`. Trong `data/benchmark_kg.json`, `must_include` của Q6 là `Cái Quang Huy`, `Lê Minh Thành`, `Pháp y tâm thần`. Cùng dòng benchmark vẫn ghi `recall=0.00 judge=2`.
- **Nguyên nhân:** `keyword_recall` chỉ tìm chuỗi con nên có thể phạt paraphrase/tên viết tắt; judge LLM lại có thể chấm rộng khi câu trả lời nghe hợp lý và cùng nhắc MDMA, dù bỏ sót một vụ. Hai phép đo đang bất đồng, không phép nào thay được kiểm tra từng sự kiện.
- **Đề xuất sửa:** dùng danh sách alias được chấp nhận cho mỗi thực thể và chấm recall theo từng ý/sự kiện; giữ judge như tín hiệu thứ hai, đồng thời kiểm tra thủ công các câu lệch giữa recall và judge. Cách này đỡ phạt cách diễn đạt khác nhưng cần định nghĩa alias và gold chi tiết hơn.

## 4. Kết luận (5 điểm)

Flat RAG đủ cho câu hỏi lấy một định nghĩa luật hoặc một chi tiết nằm gọn trong một bài: Q1 và Q2 đều đạt `1.00 / 2` ở cả hai pipeline, trong khi Graph tăng chi phí truy vấn trung bình khoảng 3.58 lần. Với câu phải nối hồ sơ tin tức tới điều luật hoặc tổng hợp nhiều vụ, KG có lợi rõ hơn: Q3–Q6 đều tăng recall so với Flat, và mean recall/judge đạt `1.00 / 2.00` so với `0.43 / 1.17`. Chọn GraphRAG khi các liên kết, điều kiện khoản luật và độ phủ tổng hợp là yêu cầu chính; nếu câu hỏi chỉ tra cứu đơn-hop, Flat RAG rẻ và nhanh hơn.

## 5. Tự kiểm (5 điểm)

```text
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.03s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[OK] KG-2 build_graph: 419 node / 616 cạnh, đường xuyên 2 KB dài 1 cạnh
[OK] KG-3 context: 4 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00080. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Sau self-check, benchmark `--judge` đã dựng lại graph đầy đủ; graph hiện tại có 477 node / 710 quan hệ. Ba ảnh Neo4j Browser nằm trong `report/img/`. Người được chọn cho `kg_my_case.png`: **Cái Quang Huy**.

## Vấn đề gặp phải (không tính điểm)

Không còn lỗi chạy chưa xử lý. E3 (trùng sự kiện giữa nguồn) và E4 (judge lệch với kiểm tra theo ý bắt buộc) là các giới hạn được tìm thấy và phân tích ở mục 3.
