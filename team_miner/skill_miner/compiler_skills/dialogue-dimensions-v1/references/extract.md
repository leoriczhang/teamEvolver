# stage=extract

完整阅读一条案例的 Metadata、Dialogue 和 Sentence index，包含全部 ranges。
识别 HWP 可迁移的可观察行为、触发情境、条件与限制。客户回应只记录实际出现的表现，
不声称销售效果、不把未经确认的权益沉淀为事实。没有可复用行为允许空 findings。

交付相对 Compile.to 路径：
analysis/dialogue_dimensions/receipts/<case_ref>-<case_version>.json

```json
{
  "schema_version": "taikang.dialogue-findings.v1",
  "case_ref": "<当前Metadata>",
  "case_version": "<当前Metadata>",
  "analysis_status": "analyzed",
  "observations": [
    {
      "local_ref": "o1",
      "label": "<行为维度名称>",
      "behavior": "<实际动作与可复用方式>",
      "applicability": {"trigger": "<触发>", "conditions": "<条件>", "limits": "<限制>"},
      "sentence_refs": ["<真实句子ID>"],
      "interpretation": "<价值分析，非引文>",
      "customer_response_refs": [],
      "notes": []
    }
  ],
  "empty_reason": "",
  "notes": []
}
```

不要照抄示例占位符。analysis_status 为 analyzed 或 analyzed_with_issues。
默认 quote_mode=none，不输出 quote，也不因缺 quote 造问题。
当 quote_mode=sentences 且确实需要引用时，可选 quote 为对象或数组，包含
case_ref、case_version、sentence_ids、char_start、char_end、text。
逐字复制完整连续句，不省略、不修正 ASR、错字、重复或换行。只给实际支持行为的句子定位。
无法确认的业务事实保留行为、标 notes；局部疑点不删除其他发现。
