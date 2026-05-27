# Contribute Broker (SAM)

A small AWS Lambda + API Gateway service that lets allowlisted GitHub users
upload derived hillshade artifacts (reprojected DEMs, hillshades, styled
TIFFs) into `s3://scriptedrelief-data/cache/` **without ever holding AWS
credentials**.

Flow:

1. CLI gets a GitHub token via the `gh` CLI (or `HILLGEN_GITHUB_TOKEN`).
2. CLI POSTs `{stage, filename, size_bytes, sha256?}` to the broker with
   `Authorization: Bearer <gh_token>`.
3. Lambda verifies the token against `https://api.github.com/user`, checks
   the username against the allowlist in S3, validates the request, and
   returns a 15-minute presigned `PUT` URL pinned to the exact `Content-Length`
   and an `x-amz-meta-contributor: gh:<username>` audit header.
4. CLI uploads directly to S3 with that URL.

Only the Lambda role can write to the bucket; contributors only see the
ephemeral URL.

---

## Stack contents

| File | Purpose |
|---|---|
| `template.yaml` | SAM template (HttpApi + Lambda + IAM) |
| `handler.py` | Lambda entry point (`lambda_handler`) |
| `requirements.txt` | Lambda runtime deps (boto3 is provided by AWS) |
| `allowlist.example.json` | Sample allowlist; copy to S3 after editing |

Allowed stages (hard-coded): `reprojected`, `hillshade`, `styled`.
DEMs are intentionally excluded — they're too large and rarely reused.

---

## One-time deploy

Requires the AWS SAM CLI (`brew install aws-sam-cli`) and AWS credentials
with permission to create Lambda + API Gateway + IAM.

```bash
cd infra/broker
sam build
sam deploy --guided
```

Suggested answers to the guided prompts:

- **Stack Name:** `hillgen-contribute-broker`
- **AWS Region:** `us-east-2` (or wherever `scriptedrelief-data` lives)
- **Parameter BucketName:** `scriptedrelief-data`
- **Parameter AllowlistKey:** `config/allowlist.json`
- **Parameter MaxBytes:** `2147483648` (2 GiB default cap)
- **Parameter PresignTtlSeconds:** `900`
- **Confirm changes before deploy:** `Y`
- **Allow SAM CLI IAM role creation:** `Y`
- **Disable rollback:** `N`
- **Save arguments to samconfig.toml:** `Y`

The output prints a `BrokerEndpoint` URL — note this; contributors use it
via the `HILLGEN_CONTRIBUTE_ENDPOINT` env var until the default DNS is
wired up.

## Upload the initial allowlist

```bash
cp allowlist.example.json allowlist.json
$EDITOR allowlist.json   # add real GitHub usernames
aws s3 cp allowlist.json s3://scriptedrelief-data/config/allowlist.json
```

The Lambda caches the allowlist for 5 minutes in-memory.

## Updating the allowlist

Edit `allowlist.json` and re-upload — same `s3 cp` command. Changes take
effect within 5 minutes (the in-memory cache TTL) or immediately on cold
starts.

## Custom domain (optional, not wired yet)

The template currently exposes only the default API Gateway URL
(`https://<id>.execute-api.<region>.amazonaws.com`). To wire
`api.scriptedrelief.com`:

1. Create / import an ACM cert for `api.scriptedrelief.com` in the same region.
2. Add an `AWS::ApiGatewayV2::DomainName` + `AWS::ApiGatewayV2::ApiMapping`
   to `template.yaml`.
3. Point a Route 53 ALIAS record at the domain's regional endpoint.
4. Re-deploy.

Until then, set the env var on contributor machines:

```bash
export HILLGEN_CONTRIBUTE_ENDPOINT='https://<api-id>.execute-api.us-east-2.amazonaws.com/v1/contribute'
```

## Observability

- Logs: CloudWatch Logs group `/aws/lambda/<function-name>`. Every grant
  emits `contribute_granted user=… stage=… key=… bytes=… sha256=…`.
- Recommended (not in template): a CloudWatch metric filter on
  `contribute_granted` with an alarm at e.g. 1000 grants/hour to catch
  runaway uploads.

## Rolling back access

To revoke a contributor: remove their entry from `allowlist.json`,
re-upload, wait ≤5 minutes. To kill the service entirely:

```bash
sam delete --stack-name hillgen-contribute-broker
```

S3 objects in `scriptedrelief-data` are unaffected.
