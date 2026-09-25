# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/versions/{id}/proof`：查看该版本当前保全证明。
- `GET /api/versions/{id}/proofs`：查看该版本历版保全证明（旧证明不删除）。
- `GET /api/archives/{id}/proofs`：查看档案内每个版本的当前证明。
- `GET /api/archives/{id}/status`：保留期限、版本状态和审计记录。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。

## 保全证明

迁移、创建副本或核验副本后，系统在同一事务内对该版本的**全部副本**重新做 SHA-256 校验，并出具一版新的保全证明（`preservation_proofs`，版本内按 `proof_no` 连续编号）。证明包含：

- `status`：`valid` / `invalid`；
- `total_copies`、`healthy_copies`、`corrupt_copies`：副本健康统计；
- `verified_at`：本次逐副本校验时间；
- `detail.failure_reasons`：失效或告警原因（无副本、损坏副本无健康来源、仍有损坏但存在可修复的健康副本等）；
- `detail.copies`：每个副本的位置、状态和逐文件失效原因。

失效规则：版本没有任何副本，或损坏副本找不到内容与清单完全一致的健康来源时，证明为 `invalid` 且版本标记为 `degraded`；修复成功后重新核验并出具新版本证明，版本恢复 `verified`。旧证明永久保留，可通过证明历史查阅；每次签发写入审计日志（`proof.issue`）。证明查询与其他接口一样受限访问控制约束：审计员只能查看自己有权访问（所有者或被授予 read/write）的档案。页面可按档案或版本查看当前证明及证明历史。
