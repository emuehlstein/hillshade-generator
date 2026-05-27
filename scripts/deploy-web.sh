#!/usr/bin/env bash
# deploy-web.sh — sync web/ to S3 and invalidate CloudFront
# Usage: bash scripts/deploy-web.sh
set -euo pipefail
export AWS_PAGER=""

BUCKET="scriptedrelief"
CF_DIST="E7XF09DLCNXI7"
WEB_DIR="$(dirname "$0")/../web"

echo "▸ Syncing web/ to s3://$BUCKET/"
aws s3 sync "$WEB_DIR/" "s3://$BUCKET/" \
  --exclude "*.DS_Store" \
  --cache-control "public, max-age=300"

echo "▸ Invalidating CloudFront distribution $CF_DIST"
aws cloudfront create-invalidation \
  --distribution-id "$CF_DIST" \
  --paths "/*" \
  --query "Invalidation.Id" --output text

echo "✓ Deployed: https://scriptedrelief.com"
echo "✓ Gallery:  https://scriptedrelief.com/gallery.html"
