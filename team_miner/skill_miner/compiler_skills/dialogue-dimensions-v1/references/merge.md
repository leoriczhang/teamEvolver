# stage=merge

完整阅读当前 window Markdown，只归并窗口内的明确 member_ref。
成员包含完整行为定义、适用情境、条件、解释与已验证来源定位。
同一观察目标、适用范围和行为方法可以归并；条件不同保留分支。
不要仅按标题相似合并，不补造产品条件、客户效果或虚构低分案例。
允许多个维度、保留独立、暂不归并和不减少数量。无需读所有历史对话或兄弟窗口。

输出路径逐字取 window.output_path，相对 Compile.to：

```json
{
  "schema_version": "taikang.dimension-merge.v1",
  "window_id": "<当前window_id>",
  "groups": [
    {
      "member_refs": ["<实际成员>"],
      "name": "<维度名称>",
      "definition": "<完整定义>",
      "observable_behaviors": ["<行为>"],
      "applicability": {},
      "notes": []
    }
  ],
  "kept_separate": [{"member_ref": "<实际成员>", "reason": "<独立原因>"}],
  "deferred": [{"member_ref": "<实际成员>", "reason": "<待核实原因>"}],
  "notes": []
}
```

每个成员至少有一个明确去向。有意多重关联可以保留，不用数量减少证明成功。
不分配全局 dimension_id；既有目标只使用宿主实际提供的 existing_dimension_id。
推断与事实分开。不确定时明确 deferred，不能丢掉成员或只返回总结。
