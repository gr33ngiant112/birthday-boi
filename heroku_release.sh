#!/bin/bash

echo "🚀 Running Heroku Release Script for Worker Dyno Setup..."

# Scale worker dyno (ensures it starts automatically after deployment)
echo "🔧 Scaling worker dyno to 1..."
heroku ps:scale worker=1 -a $HEROKU_APP_NAME

# Restart the app to ensure changes take effect
echo "🔄 Restarting app..."
heroku restart -a $HEROKU_APP_NAME

echo "✅ Heroku worker dyno setup complete!"
