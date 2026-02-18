# 🏗️ System Architecture Overview

> High-level view of all system components, their relationships, and data flow between layers.

## Overview

The Remnawave Bedolaga Bot is a comprehensive Telegram-based VPN subscription management system. It integrates with multiple external services for payments, VPN backend management, tax compliance, and security.

## Architecture Diagram

```mermaid
flowchart TB
    subgraph Users["👥 Users"]
        TGUser[Telegram User]
        WebUser[Web Cabinet User]
        AdminUser[Admin User]
    end

    subgraph TelegramBot["🤖 Telegram Bot Core"]
        subgraph EntryPoints["Entry Points"]
            Bot[Aiogram Bot]
            WebhookServer[Webhook Server]
            MiniApp[Web Application Interface]
        end
        
        subgraph Middlewares["Middleware Chain"]
            AuthMiddleware[Check user identity]
            RateLimitMiddleware[Rate Limit]
            BlacklistMiddleware[Blacklist Check]
            MaintenanceMiddleware[Maintenance Mode]
            ChannelMiddleware[Channel Checker]
            LoggingMiddleware[Logging]
        end
        
        subgraph Handlers["Handlers Layer"]
            StartHandler[Start Handler]
            MenuHandler[Menu processing]
            SubscriptionHandler[Subscription Handlers]
            PaymentHandler[Payment Handlers]
            AdminHandler[Admin commands processing]
            SupportHandler[Support Handlers]
            ReferralH[Referral Handlers]
        end
        
        subgraph Services["Services Layer"]
            SubService[Subscription management]
            PaymentService[Payment processing]
            UserService[User management]
            RemnaWaveService[VPN management]
            NotifyService[Notification system]
            PromoService[Promocode Service]
            ReferralService[Referral program]
            BackupService[Backup system]
        end
        
        subgraph DataLayer["Data Layer"]
            Database[(PostgreSQL)]
            Redis[(Redis Cache)]
            FSM[State Storage]
        end
    end

    subgraph ExternalPayments["💳 Payment Providers"]
        subgraph NativePayments["Native"]
            TGStars[Telegram Stars]
        end
        subgraph CryptoPayments["Crypto"]
            CryptoBot[CryptoBot]
            Heleket[Heleket]
        end
        subgraph FiatPayments["Fiat RU"]
            YooKassa[YooKassa]
            MulenPay[MulenPay]
            PAL24[PAL24]
            Platega[Platega]
            WATA[WATA]
            Freekassa[Freekassa]
            CloudPay[CloudPayments]
        end
        subgraph DonationPayments["Donations"]
            Tribute[Tribute]
        end
    end

    subgraph VPNBackend["🔐 VPN Backend"]
        RemnaWave[RemnaWave API]
        RemnaWebhook[RemnaWave Webhooks]
        subgraph VPNInfra["Infrastructure"]
            Squad1[EU Servers]
            Squad2[Asia Servers]
            Squad3[US Servers]
        end
    end

    subgraph ExternalServices["🌐 External Services"]
        NaloGO[NaloGO Tax]
        BanSystem[Ban System API]
        Blacklist[GitHub Blacklist]
        EmailCheck[Disposable Email]
    end

    subgraph Notifications["📢 Admin Notifications"]
        AdminChat[Admin Group]
        subgraph Topics["Forum Topics"]
            PaymentTopic[Payments]
            TicketTopic[Tickets]
            SystemTopic[System]
            ErrorTopic[Errors]
        end
    end

    %% User connections
    TGUser --> Bot
    WebUser --> MiniApp
    AdminUser --> Bot
    AdminUser --> MiniApp
    
    %% Internal flow
    Bot --> AuthMiddleware --> RateLimitMiddleware --> BlacklistMiddleware --> MaintenanceMiddleware --> ChannelMiddleware --> LoggingMiddleware --> Handlers
    WebhookServer --> Services
    MiniApp --> Services
    
    Handlers --> Services
    Services --> DataLayer
    Services --> RemnaWaveService
    RemnaWaveService --> RemnaWave
    RemnaWave --> VPNInfra
    RemnaWebhook --> WebhookServer
    
    %% Payment connections
    PaymentService --> ExternalPayments
    ExternalPayments --> WebhookServer
    
    %% External services
    Services --> NaloGO
    BlacklistMiddleware --> Blacklist
    AuthMiddleware --> BanSystem
    UserService --> EmailCheck
    
    %% Notifications
    NotifyService --> AdminChat
    AdminChat --> Topics
```

## Detailed Component Diagram

```mermaid
flowchart LR
    subgraph Database["💾 PostgreSQL Tables"]
        Users[(users)]
        Subs[(subscriptions)]
        Trans[(transactions)]
        Tickets[(tickets)]
        Promos[(promo_codes)]
        Tariffs[(tariffs)]
        PromoGroups[(promo_groups)]
        Campaigns[(campaigns)]
    end
    
    subgraph Redis["⚡ Redis Keys"]
        Cart[cart:user ID]
        UserState[state:user ID]
        RateLimit[rate:user ID]
        NotifCache[notif:user ID]
        TrafficSnap[traffic:user ID]
        Session[session:token]
    end
    
    Users --> Subs
    Users --> Trans
    Users --> Tickets
    Trans --> Promos
    Subs --> Tariffs
    Users --> PromoGroups
    Users --> Campaigns
```

## Component Description

### 👥 Users Layer

| Component | Description |
|-----------|-------------|
| **Telegram User** | End users interacting via Telegram bot interface |
| **Web Cabinet User** | Users accessing the web-based cabinet (MiniApp) |

### 🤖 Telegram Bot Layer

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Aiogram Bot** | Aiogram 3.x | Async Telegram bot framework handling all user interactions |
| **Handlers Layer** | Python modules | Route messages/callbacks to appropriate business logic |
| **Services Layer** | Python services | Core business logic, orchestration, and external API calls |
| **PostgreSQL** | PostgreSQL 15+ | Primary data storage for users, subscriptions, transactions |
| **Redis Cache** | Redis 7+ | Session storage, rate limiting, caching, cart persistence |

### 💳 Payment Integrations

| Provider | Type | Primary Use Case |
|----------|------|------------------|
| **Telegram Stars** | Native | In-app purchases via Telegram's native payment system |
| **Tribute** | Donation | Telegram-native donation/tip system |
| **CryptoBot** | Crypto | Cryptocurrency payments (USDT, TON, BTC, ETH) |
| **Heleket** | Crypto | Alternative crypto payment gateway |
| **YooKassa** | Fiat | Russian bank cards and SBP (fast payments) |
| **MulenPay** | Fiat | Alternative fiat payment processing |
| **PAL24** | Fiat | SBP and card payments |
| **Platega** | Fiat | Multi-method payment gateway |
| **WATA** | Fiat | Payment processing service |
| **Freekassa** | Fiat | NSPK SBP and card payments |
| **CloudPayments** | Fiat | Cards and SBP integration |

### 🔐 VPN Backend

| Component | Purpose |
|-----------|---------|
| **RemnaWave API** | Central VPN management - user provisioning, subscription control, traffic monitoring |
| **VPN Servers/Squads** | Actual VPN server infrastructure organized in "squads" (server groups) |

### 🌐 External Services

| Service | Purpose |
|---------|---------|
| **NaloGO Tax Service** | Russian 54-FZ tax receipt generation and submission |
| **Ban System API** | Centralized ban management across multiple bots/services |
| **Blacklist Service** | GitHub-hosted blacklist for fraud prevention |

### 📢 Notifications

| Component | Purpose |
|-----------|---------|
| **Admin Telegram Group** | Central notification hub for administrators |
| **Forum Topics** | Organized notification channels (payments, support, system alerts) |

## Data Flow Summary

1. **User Input** → Bot receives messages/callbacks from Telegram
2. **Handler Processing** → Appropriate handler routes the request
3. **Service Logic** → Business services process the request
4. **Data Persistence** → PostgreSQL stores persistent data, Redis handles sessions
5. **External Calls** → Services communicate with RemnaWave, payment providers, etc.
6. **Response** → Bot sends response back to user
7. **Notifications** → Admin group receives relevant notifications

## Key Design Principles

- **Async-First**: All I/O operations are asynchronous for maximum throughput
- **Modular Services**: Each service handles a specific domain (payments, subscriptions, etc.)
- **External Abstraction**: Payment providers are abstracted behind a unified interface
- **Event-Driven**: Webhooks enable real-time updates from external services
- **Caching Strategy**: Redis reduces database load and improves response times

---

**Related Diagrams:**
- [Data Flow Diagram](./12-data-flow.md)
- [External Integrations Map](./11-external-integrations.md)

