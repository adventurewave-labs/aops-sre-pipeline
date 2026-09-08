// api/trigger-alert.js
// A.O.P.S. Live Alert Trigger — Vercel Serverless Function
//
// Executes the A.O.P.S. sandbox pipeline and returns structured results
// matching the exact schema the real Python pipeline produces.
//
// Pipeline stages derived from the real A.O.P.S. Python services:
//   - services/popeye-scanner/scanner.py  (14 analyzer codes, Popeye-shaped JSON)
//   - services/remediation-executor/app.py (allowlisted kubectl verbs)
//   - services/mock-slack/app.py           (Slack incoming-webhook format)
//   - services/n8n-runner/app.py           (workflow graph execution)
//   - scripts/run_uat.py                  (T03-T15 acceptance criteria)
//
// The sandbox completes in ~55ms total. Since we can't run Docker services
// inside Vercel serverless, we return pre-computed sandbox results with the
// same schema the real pipeline emits.

const crypto = require('crypto');

export default async function handler(req, res) {
  // CORS preflight
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }

  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const runId = `run-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`;

  // ---------------------------------------------------------------------------
  // Pipeline stages — each has a terminal-friendly `output` string for the
  // live UI panel, plus structured `detail` for programmatic consumers.
  //
  // Data sourced from:
  //   scanner.py:  14 Popeye codes, score 28, grade F, 15 findings (8 error, 4 warn, 3 info)
  //   app.py (remediation): 5 allowlisted verbs (set-image, set-resources, create-storageclass, set-ingress-backend, inspect-nodes)
  //   app.py (slack):  incoming-webhook format, severity critical, channel #sre-alerts
  //   run_uat.py:  T03 expects codes {NO-002,DPL-000,DPL-001,POP-001,POP-002,MEM-001,ING-001,PVC-001,PVC-002}
  //                T08 expects E2E latency <2s (sandbox ~55ms)
  // ---------------------------------------------------------------------------

  const stages = [
    {
      name: 'prometheus',
      duration_ms: 3,
      status: 'success',
      output: 'Alert: PaymentAPIHighErrorRate on payment-prod (severity: critical)',
      detail: {
        alert: 'PaymentAPIHighErrorRate',
        severity: 'critical',
        namespace: 'payment-prod',
        webhook: 'alertmanager → n8n',
      },
    },
    {
      name: 'n8n-workflow',
      duration_ms: 2,
      status: 'success',
      output: 'Workflow aops-sre-pipeline triggered (6 nodes)',
      detail: {
        workflow: 'aops-sre-pipeline',
        triggered: true,
        nodes: [
          'Alertmanager Webhook',
          'Popeye Scan',
          'Dify Agent Reasoning',
          'Post to Slack',
          'Execute Remediation',
          'Respond to Alertmanager',
        ],
      },
    },
    {
      name: 'popeye',
      duration_ms: 18,
      status: 'success',
      output: '15 findings (8E/4W/3I): POP-002 CrashLoop, PVC-001 Pending, SVC-001 NoEndpoints, ING-001 Dangling, MEM-001 OOMKilled…',
      detail: {
        scanner: 'popeye',
        popeye_version: 'builtin-analyzers',
        aops_mode: 'sandbox',
        data_source: 'fixtures',
        engine: 'builtin-analyzers',
        namespace: 'payment-prod',
        score: 28,
        grade: 'F',
        findings_count: 15,
        findings_by_severity: { error: 8, warning: 4, info: 3 },
        codes: [
          'NO-002', 'DPL-000', 'DPL-001',
          'POP-001', 'POP-002', 'MEM-001',
          'ING-001', 'PVC-001', 'PVC-002',
        ],
      },
    },
    {
      name: 'ollama-llm',
      duration_ms: 12,
      status: 'success',
      output: 'Analysis: Memory pressure → set-resources; Dangling Ingress → set-ingress-backend; Missing SC → create-storageclass',
      detail: {
        model: 'qwen2.5:0.5b',
        backend: 'stub',
        agent_rounds: 2,
        analysis: 'Memory pressure on payment-api pods suggests resource limit adjustment needed. Dangling Ingress routes to non-existent Service. Pending PVC references missing StorageClass.',
        plan: {
          generated_by: 'dify-lite/stub',
          step_count: 3,
          verbs: ['set-resources', 'set-ingress-backend', 'create-storageclass'],
        },
      },
    },
    {
      name: 'kubectl-remediate',
      duration_ms: 9,
      status: 'success',
      output: '3 patches applied (DRY_RUN): deployment/payment-api, ingress/payment-ingress, storageclass/local-storage',
      detail: {
        dry_run: true,
        namespace: 'payment-prod',
        data_source: 'fixtures',
        steps_applied: 3,
        steps_rejected: 0,
        steps_failed: 0,
        before: { score: 28, grade: 'F', findings_count: 15 },
        after: { score: 95, grade: 'A', findings_count: 2 },
        improvement: { score_delta: 67, findings_delta: 13 },
        allowed_verbs: [
          'set-image', 'set-resources',
          'create-storageclass', 'set-ingress-backend', 'inspect-nodes',
        ],
      },
    },
    {
      name: 'slack-notify',
      duration_ms: 5,
      status: 'success',
      output: 'Notification sent to #sre-alerts (severity: critical, score: 28→95)',
      detail: {
        channel: '#sre-alerts',
        alert_name: 'PaymentAPIHighErrorRate',
        severity: 'critical',
        score_before: 28,
        score_after: 95,
        grade_before: 'F',
        grade_after: 'A',
        data_source: 'fixtures',
      },
    },
  ];

  const totalDuration = stages.reduce((sum, s) => sum + s.duration_ms, 0);

  // Summary matching the real remediation-executor before/after report
  const summary = {
    findings_detected: 15,
    findings_by_severity: { error: 8, warning: 4, info: 3 },
    remediations_applied: 3,
    health_before: 28,
    health_before_grade: 'F',
    health_after: 95,
    health_after_grade: 'A',
  };

  // Slack card matching mock-slack/app.py envelope format
  const slack_card = {
    title: 'PaymentAPIHighErrorRate — payment-prod',
    status: 'critical',
    findings: 15,
    remediated: 3,
    score_before: 28,
    score_after: 95,
    grade_before: 'F',
    grade_after: 'A',
    duration_ms: totalDuration,
    data_source: 'fixtures',
    channel: '#sre-alerts',
  };

  return res.status(200).json({
    run_id: runId,
    mode: 'sandbox',
    aops_mode: 'sandbox',
    data_source: 'fixtures',
    total_duration_ms: totalDuration,
    stages,
    summary,
    slack_card,
    timestamp: new Date().toISOString(),
  });
}
