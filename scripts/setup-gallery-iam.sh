#!/usr/bin/env bash
# setup-gallery-iam.sh
# Creates the hillgen-gallery-contributors IAM user with write-only access
# to s3://scriptedrelief/gallery/ and issues a shared access key.
#
# Run once after `aws login`. Outputs the key to stdout — share with friends.
#
# Usage:
#   aws login   # re-auth first
#   bash scripts/setup-gallery-iam.sh

set -euo pipefail

BUCKET="scriptedrelief"
PREFIX="gallery/"
USER="hillgen-gallery-contributors"
POLICY_NAME="hillgen-gallery-write"

echo "=== hillgen gallery IAM setup ==="
echo ""

# ── 1. Create user if not exists ────────────────────────────────────────────
if aws iam get-user --user-name "$USER" &>/dev/null; then
  echo "✓ IAM user already exists: $USER"
else
  echo "▸ Creating IAM user: $USER"
  aws iam create-user --user-name "$USER" \
    --tags Key=Purpose,Value=hillgen-gallery-contributors
  echo "✓ Created: $USER"
fi

# ── 2. Write and attach inline policy ───────────────────────────────────────
POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GalleryUpload",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:PutObjectAcl"
      ],
      "Resource": "arn:aws:s3:::${BUCKET}/${PREFIX}*"
    },
    {
      "Sid": "GalleryListOwn",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::${BUCKET}",
      "Condition": {
        "StringLike": {
          "s3:prefix": "${PREFIX}*"
        }
      }
    }
  ]
}
EOF
)

echo "▸ Attaching inline policy: $POLICY_NAME"
aws iam put-user-policy \
  --user-name "$USER" \
  --policy-name "$POLICY_NAME" \
  --policy-document "$POLICY_DOC"
echo "✓ Policy attached"

# ── 3. Delete any existing keys (keep at most 1) ────────────────────────────
EXISTING=$(aws iam list-access-keys --user-name "$USER" \
  --query 'AccessKeyMetadata[*].AccessKeyId' --output text)
for key_id in $EXISTING; do
  echo "▸ Deleting old key: $key_id"
  aws iam delete-access-key --user-name "$USER" --access-key-id "$key_id"
done

# ── 4. Create new access key ─────────────────────────────────────────────────
echo "▸ Creating access key..."
KEY_JSON=$(aws iam create-access-key --user-name "$USER")
ACCESS_KEY=$(echo "$KEY_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['AccessKey']['AccessKeyId'])")
SECRET_KEY=$(echo "$KEY_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['AccessKey']['SecretAccessKey'])")

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  hillgen gallery contributor key"
echo "  Share this with friends — write-only to gallery/"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  AWS_ACCESS_KEY_ID=$ACCESS_KEY"
echo "  AWS_SECRET_ACCESS_KEY=$SECRET_KEY"
echo "  AWS_DEFAULT_REGION=us-east-2"
echo ""
echo "  hillgen publish --gallery path/to/output.pmtiles"
echo ""
echo "═══════════════════════════════════════════════════════"
echo ""
echo "⚠  This is the only time the secret key is shown."
echo "   Save it now."
