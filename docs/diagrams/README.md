# 📊 Remnawave Bedolaga Bot - Architecture & Flow Diagrams

> Comprehensive documentation of the system architecture, user flows, business processes, and external integrations.

## 📑 Table of Contents

### Architecture & System Design
| # | Diagram | Description |
|---|---------|-------------|
| 1 | [System Architecture](./01-system-architecture.md) | High-level overview of all system components and their interactions |
| 12 | [Data Flow](./12-data-flow.md) | How data moves through the system from input to output |
| 11 | [External Integrations](./11-external-integrations.md) | Map of all external services and APIs |

### User Flows
| # | Diagram | Description |
|---|---------|-------------|
| 2 | [User Registration & Onboarding](./02-user-registration.md) | Complete user journey from /start to main menu |
| 5 | [Trial Activation](./05-trial-activation.md) | Free trial activation process and eligibility checks |
| 13 | [Complete User Journey](./13-user-journey.md) | End-to-end user experience visualization |

### Subscription & Payments
| # | Diagram | Description |
|---|---------|-------------|
| 3 | [Subscription Purchase (Classic)](./03-subscription-purchase-classic.md) | Traditional subscription flow with customizable options |
| 4 | [Subscription Purchase (Tariff)](./04-subscription-purchase-tariff.md) | Simplified tariff-based subscription purchase |
| 6 | [Payment Processing](./06-payment-processing.md) | All payment methods and processing flows |
| 7 | [Subscription Renewal & Auto-Pay](./07-subscription-renewal.md) | Automatic and manual renewal processes |

### Additional Features
| # | Diagram | Description |
|---|---------|-------------|
| 8 | [Referral System](./08-referral-system.md) | Referral program mechanics and rewards |
| 9 | [Admin Management](./09-admin-management.md) | Administrative panel and management features |
| 10 | [Support Ticket System](./10-support-ticket.md) | Customer support workflow with SLA tracking |

---

## 🔗 Quick Links

### By User Role

**👤 End Users:**
- [Registration Flow](./02-user-registration.md)
- [Trial Activation](./05-trial-activation.md)
- [Subscription Purchase](./03-subscription-purchase-classic.md)
- [Payment Methods](./06-payment-processing.md)
- [Referral Program](./08-referral-system.md)

**👨‍💼 Administrators:**
- [Admin Panel](./09-admin-management.md)
- [Support Tickets](./10-support-ticket.md)
- [System Architecture](./01-system-architecture.md)

**👨‍💻 Developers:**
- [System Architecture](./01-system-architecture.md)
- [Data Flow](./12-data-flow.md)
- [External Integrations](./11-external-integrations.md)

---

## 🛠️ External Integrations Summary

### 💳 Payment Providers (11 integrations)
| Provider | Type | Features |
|----------|------|----------|
| Telegram Stars | Native | Built-in Telegram payments |
| CryptoBot | Crypto | USDT, TON, BTC, ETH via Telegram |
| Heleket | Crypto | Alternative crypto gateway |
| YooKassa | Fiat | Russian cards + SBP |
| MulenPay | Fiat | Alternative payment provider |
| PAL24 | Fiat | SBP + Card payments |
| Platega | Fiat | Cards + SBP |
| WATA | Fiat | Payment gateway |
| Freekassa | Fiat | NSPK SBP + Cards |
| CloudPayments | Fiat | Cards + SBP |
| Tribute | Donation | Telegram donation system |

### 🔐 VPN Backend
| Service | Purpose |
|---------|---------|
| RemnaWave API | User management, subscription control, server configuration |
| RemnaWave Webhooks | Real-time events (expiry, limits, status changes) |

### 📋 Tax & Compliance
| Service | Purpose |
|---------|---------|
| NaloGO | Russian tax receipt generation (54-FZ compliance) |

### 🛡️ Security Services
| Service | Purpose |
|---------|---------|
| Ban System API | Centralized user blocking |
| Blacklist Service | GitHub-based blacklist synchronization |
| Disposable Email Check | Prevent fake registrations |

### 💾 Data Storage
| Service | Purpose |
|---------|---------|
| PostgreSQL | Primary database |
| Redis | Caching, sessions, rate limiting |

---

## 📖 How to Read These Diagrams

All diagrams use [Mermaid](https://mermaid.js.org/) syntax and can be rendered by:
- GitHub (native support)
- GitLab (native support)
- VS Code with Mermaid extension
- Any Mermaid-compatible viewer

### Color Legend

| Color | Meaning |
|-------|---------|
| 🟢 Green (`#4CAF50`) | Start point / Entry |
| 🔵 Blue (`#2196F3`) | End point / Success |
| 🟠 Orange (`#FF9800`) | Scheduled / Automated |
| 🟣 Purple (`#9C27B0`) | Admin / Privileged |
| 🔴 Red (`#f44336`) | Error / Failure |

### Symbol Legend

| Symbol | Meaning |
|--------|---------|
| 🤖 | Telegram Bot |
| 💳 | Payment System |
| 🔐 | VPN/Security |
| 📢 | Notifications |
| 💾 | Database/Storage |
| ⚙️ | Processing |
| 👥 | Users |
| 🏆 | Contests/Rewards |
| 📱 | Mobile/Telegram |
| ☁️ | Cloud Services |

---

## 📁 File Structure

```
docs/diagrams/
├── README.md                          # This file - main index
├── 01-system-architecture.md          # System overview
├── 02-user-registration.md            # Onboarding flow
├── 03-subscription-purchase-classic.md # Classic purchase
├── 04-subscription-purchase-tariff.md  # Tariff purchase
├── 05-trial-activation.md             # Trial flow
├── 06-payment-processing.md           # All payments
├── 07-subscription-renewal.md         # Renewal & auto-pay
├── 08-referral-system.md              # Referral program
├── 09-admin-management.md             # Admin panel
├── 10-support-ticket.md               # Support system
├── 11-external-integrations.md        # Integration map
├── 12-data-flow.md                    # Data flow
└── 13-user-journey.md                 # User journey
```

---

## 🔄 Keeping Diagrams Updated

When making changes to the system:

1. **New Feature** → Update relevant flow diagram
2. **New Integration** → Update [External Integrations](./11-external-integrations.md)
3. **Architecture Change** → Update [System Architecture](./01-system-architecture.md)
4. **New Payment Method** → Update [Payment Processing](./06-payment-processing.md)

---

*Last updated: February 2026*

