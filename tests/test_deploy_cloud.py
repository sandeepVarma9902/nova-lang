#!/usr/bin/env python3
# test_deploy_cloud.py — unit tests for nova_lang/deploy_cloud.py.
#
# Generates AWS + GCP artifacts into a temp dir from a fixture app.nova
# and asserts the key files/commands exist. Run directly:
#   python3 tests/test_deploy_cloud.py

import json
import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nova_lang.deploy_cloud import generate_aws, generate_gcp  # noqa: E402
from nova_lang.manifest import load_manifest  # noqa: E402


APP_NOVA = '''\
app "fixture-app" {
  version = "0.2.1"
  entry = "main.nova"
  threading = "auto"

  env prod { log_level = "info" }

  resource secret "db-password" { store = "env" }
  resource cron "nightly-report" { schedule = "0 2 * * *", run = "report.nova" }

  docker { base = "python:3.12-slim", port = 8080 }
}
'''

MAIN_NOVA = 'let greeting: String = "hi"\\n'


class DeployCloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.appdir = os.path.join(self.tmp, "app")
        os.makedirs(self.appdir)
        for name, src in (("app.nova", APP_NOVA), ("main.nova", MAIN_NOVA),
                          ("report.nova", MAIN_NOVA)):
            with open(os.path.join(self.appdir, name), "w") as fh:
                fh.write(src)
        self.man_path = os.path.join(self.appdir, "app.nova")
        self.man = load_manifest(self.man_path)

    def _read(self, path):
        with open(path) as fh:
            return fh.read()

    # ---- AWS ---------------------------------------------------------
    def test_aws_writes_expected_files(self):
        out = os.path.join(self.tmp, "deploy-aws")
        written = generate_aws(self.man, self.man_path, out)
        names = sorted(os.path.basename(p) for p in written)
        self.assertEqual(names, ["Dockerfile", "deploy.sh",
                                "service.json", "task-definition.json"])
        for p in written:
            self.assertTrue(os.path.isfile(p), p)

    def test_aws_dockerfile(self):
        out = os.path.join(self.tmp, "deploy-aws")
        generate_aws(self.man, self.man_path, out)
        df = self._read(os.path.join(out, "Dockerfile"))
        self.assertIn("FROM python:3.12-slim", df)
        self.assertIn("COPY nova_lang /opt/nova-lang/nova_lang", df)
        self.assertIn("COPY main.nova /app/main.nova", df)
        self.assertIn("COPY report.nova /app/report.nova", df)
        self.assertIn('CMD ["nova", "run", "main.nova"]', df)
        self.assertIn("ENV NOVA_THREADING=auto", df)

    def test_aws_task_definition(self):
        out = os.path.join(self.tmp, "deploy-aws")
        generate_aws(self.man, self.man_path, out)
        td = json.loads(self._read(
            os.path.join(out, "task-definition.json")))
        self.assertEqual(td["family"], "fixture-app")
        self.assertEqual(td["requiresCompatibilities"], ["FARGATE"])
        c = td["containerDefinitions"][0]
        env = {e["name"]: e["value"] for e in c["environment"]}
        self.assertEqual(env["LOG_LEVEL"], "info")
        secrets = {s["name"]: s["valueFrom"] for s in c["secrets"]}
        self.assertIn("NOVA_SECRET_DB_PASSWORD", secrets)
        self.assertIn("FILL_IN",
                      secrets["NOVA_SECRET_DB_PASSWORD"])

    def test_aws_deploy_sh(self):
        out = os.path.join(self.tmp, "deploy-aws")
        generate_aws(self.man, self.man_path, out)
        sh = self._read(os.path.join(out, "deploy.sh"))
        mode = os.stat(os.path.join(out, "deploy.sh")).st_mode
        self.assertTrue(mode & stat.S_IXUSR, "deploy.sh must be executable")
        # EventBridge Scheduler for the cron resource, with the AWS-style
        # cron expression (5-field '0 2 * * *' -> cron(0 2 * * ? *))
        self.assertIn("aws scheduler create-schedule", sh)
        self.assertIn("--schedule-expression \"cron(0 2 * * ? *)\"", sh)
        self.assertIn("fixture-app-nightly-report", sh)
        self.assertIn('["nova", "run", "report.nova"]', sh)
        # core ECS pipeline
        self.assertIn("register-task-definition", sh)
        self.assertIn("create-service", sh)
        self.assertIn("aws ecr", sh)
        # honest placeholders + guards
        self.assertIn("FILL_IN", sh)
        self.assertIn("aws configure", sh)

    # ---- GCP ---------------------------------------------------------
    def test_gcp_writes_expected_files(self):
        out = os.path.join(self.tmp, "deploy-gcp")
        written = generate_gcp(self.man, self.man_path, out)
        names = sorted(os.path.basename(p) for p in written)
        self.assertEqual(names, ["Dockerfile", "deploy.sh",
                                "service.yaml"])
        for p in written:
            self.assertTrue(os.path.isfile(p), p)

    def test_gcp_dockerfile(self):
        out = os.path.join(self.tmp, "deploy-gcp")
        generate_gcp(self.man, self.man_path, out)
        df = self._read(os.path.join(out, "Dockerfile"))
        self.assertIn("FROM python:3.12-slim", df)
        self.assertIn('CMD ["nova", "run", "main.nova"]', df)

    def test_gcp_deploy_sh(self):
        out = os.path.join(self.tmp, "deploy-gcp")
        generate_gcp(self.man, self.man_path, out)
        sh = self._read(os.path.join(out, "deploy.sh"))
        mode = os.stat(os.path.join(out, "deploy.sh")).st_mode
        self.assertTrue(mode & stat.S_IXUSR, "deploy.sh must be executable")
        self.assertIn("gcloud run deploy", sh)
        self.assertIn("gcloud scheduler jobs create http", sh)
        # standard 5-field cron passes through to Cloud Scheduler
        self.assertIn("--schedule=\"0 2 * * *\"", sh)
        self.assertIn("fixture-app-nightly-report", sh)
        self.assertIn("gcloud auth login", sh)
        self.assertIn("FILL_IN", sh)

    def test_gcp_service_yaml(self):
        out = os.path.join(self.tmp, "deploy-gcp")
        generate_gcp(self.man, self.man_path, out)
        y = self._read(os.path.join(out, "service.yaml"))
        self.assertIn("kind: Service", y)
        self.assertIn("name: fixture-app", y)
        self.assertIn("%IMAGE_URI%", y)
        self.assertIn('value: "info"', y)


if __name__ == "__main__":
    unittest.main(verbosity=1)
