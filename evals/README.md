# Eval harness

用已知根因的 Issue 当标尺，检查调查结果是否对得上，而不是只看单测覆盖率。

## 目录

- `cases/*.json` — 用例
- `score.py` — 离线打分（不访问网络）
- `run_eval.py` — CLI（真实跑 Agent 需要 `OPENAI_API_KEY`）

## 用例格式

```json
{
  "id": "my-case-001",
  "issue_url": "https://github.com/owner/repo/issues/123",
  "expected": {
    "root_cause_keywords": ["null pointer", "missing nil check"],
    "evidence_path_substrings": ["src/auth/", "handlers/"],
    "min_confidence": "medium",
    "require_patch": false
  }
}
```

打分规则：

| 检查项 | 通过条件 |
|---|---|
| `root_cause_keywords` | `summary` 或 `root_cause` 命中任一关键词（大小写不敏感）；关键词为空则跳过 |
| `evidence_path_substrings` | 任一证据路径包含任一子串；列表为空则跳过 |
| `min_confidence` | 实际置信度 ≥ 要求等级 |
| `require_patch` | 为 `true` 时报告必须含非空 patch |

## 运行

```bash
# 打分逻辑单测（无网络）
pytest tests/test_eval_score.py -q

# 真实评测（需 .env 中的 OPENAI_API_KEY，并能访问 GitHub）
python -m evals.run_eval --dir evals/cases
python -m evals.run_eval --case evals/cases/your-case.json
```

先放 5–10 个你自己核对过根因的已关闭 Issue 做基线；之后改 prompt、换模型或调超时，对比通过率和 `estimated_cost_usd`。
