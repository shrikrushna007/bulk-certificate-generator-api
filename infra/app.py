from __future__ import annotations

import os

import aws_cdk as cdk

from infra.certflow_stack import CertFlowStack

app = cdk.App()
CertFlowStack(
    app,
    "CertFlowProduction",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    termination_protection=True,
)
app.synth()
