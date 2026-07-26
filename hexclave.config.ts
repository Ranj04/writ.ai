import type { HexclaveConfig } from "@hexclave/js";

export const config: HexclaveConfig = {
  "emails": {
    "selectedThemeId": "a0172b5d-cff0-463b-83bb-85124697373a"
  },
  "apiKeys": {
    "enabled": {
      "user": true
    }
  },
  "auth": {
    "otp": {
      "allowSignIn": true
    },
    "password": {
      "allowSignIn": true
    }
  },
  "apps": {
    "installed": {
      "rbac": { "enabled": true },
      "teams": { "enabled": true },
      "emails": { "enabled": true },
      "api-keys": { "enabled": true },
      "payments": { "enabled": true },
      "webhooks": { "enabled": true },
      "analytics": { "enabled": true },
      "authentication": { "enabled": true }
    }
  },
  "rbac": {
    "permissions": {
      "approve_compliance": {
        "description": "Approve compliance-scoped Dragback decision changes",
        "scope": "team"
      }
    }
  }
};
