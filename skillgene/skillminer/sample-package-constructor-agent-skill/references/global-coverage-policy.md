# Global Coverage Policy

不要只看单个包。  
必须在全部样本包的并集上尽量实现高覆盖。

## 目标
1. 大部分高价值文件被至少一个包使用到
2. 大部分高价值片段被至少一个包吸收
3. 关键片段允许被多个包复用
4. 未被使用的数据尽量是冗余、低价值或当前不重要的
5. 跨全部包的并集吸收率（并集片段量 / 簇原文总量）默认 >= 25%，
   远低于此说明每包切得太浅（详见 slice-depth-policy.md）

## 必须输出
- `global_notes/coverage_report.md`
- `global_notes/unused_or_low_priority_data.md`


## 目录来源专项要求
对于每一个目录来源，还要额外检查：
- 是否每个样本包都采到了该目录中的足够数量文件
- 跨全部样本包的并集，是否覆盖了该目录中 80% 以上的文件
- 若未达到，是否已在 `unused_or_low_priority_data.md` 中说明原因
