#!/bin/bash

echo "🚀 Running Heroku Release Script for Worker Dyno Setup..."

# Ensure HEROKU_APP_NAME is set
if [ -z "$HEROKU_APP_NAME" ]; then
  echo "❌ ERROR: HEROKU_APP_NAME is not set! Exiting..."
  exit 1
fi

# Ensure HEROKU_API_KEY is set
if [ -z "$HEROKU_API_KEY" ]; then
  echo "❌ ERROR: HEROKU_API_KEY is not set! Exiting..."
  exit 1
fi

# Scale worker dyno to 1 using Heroku API
echo "🔧 Scaling worker dyno to 1..."
curl -n -X PATCH "https://api.heroku.com/apps/$HEROKU_APP_NAME/formation/worker" \
-H "Accept: application/vnd.heroku+json; version=3" \
-H "Authorization: Bearer $HEROKU_API_KEY" \
-H "Content-Type: application/json" \
-d '{"quantity":1}'

# Restart the app using Heroku API
echo "🔄 Restarting app..."
curl -n -X DELETE "https://api.heroku.com/apps/$HEROKU_APP_NAME/dynos" \
-H "Accept: application/vnd.heroku+json; version=3" \
-H "Authorization: Bearer $HEROKU_API_KEY"

echo "✅ Heroku worker dyno setup complete!"
