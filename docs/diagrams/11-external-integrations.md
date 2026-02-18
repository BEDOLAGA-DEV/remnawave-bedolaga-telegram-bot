# 🔌 External Integrations Map

> Comprehensive map of all external services, APIs, and their connections to the bot core.

## Overview

The Remnawave Bedolaga Bot integrates with numerous external systems to provide comprehensive VPN subscription management, payment processing, tax compliance, and security features.

## Complete Integration Map

```mermaid
flowchart TB
    subgraph Users["👥 User Entry Points"]
        TGUser[Telegram User]
        WebUser[Web Cabinet User]
        AdminUser[Admin User]
    end

    subgraph BotCore["🤖 Bedolaga Bot Core"]
        subgraph Handlers["Request Handlers"]
            StartHandler[Start Handler]
            PaymentHandler[Payment Handler]
            SubscriptionHandler[Subscription Handler]
            AdminHandler[Admin commands processing]
            WebhookHandler[Webhook Handler]
        end
        
        subgraph Services["Business Services"]
            UserService[User management]
            SubscriptionService[Subscription management]
            PaymentSvc[Payment processing]
            RemnaWaveService[VPN management]
            NotificationService[Notification system]
        end
    end

    subgraph VPNBackend["🔐 VPN Backend"]
        subgraph RemnaWaveAPI["RemnaWave API"]
            UserMgmt[User Management]
            SubMgmt[Subscription Management]
            ServerMgmt[Server/Squad Management]
            TrafficMgmt[Traffic Monitoring]
        end
        RemnaWebhook[RemnaWave Webhooks]
        subgraph Servers["VPN Infrastructure"]
            EU[EU Servers]
            Asia[Asia Servers]
            US[US Servers]
        end
    end

    subgraph PaymentProviders["💳 Payment Providers"]
        subgraph Native["Native Payments"]
            TGStars[Telegram Stars]
        end
        subgraph Crypto["Cryptocurrency"]
            CryptoBot[CryptoBot<br/>USDT, TON, BTC, ETH]
            Heleket[Heleket<br/>USDT, BTC]
        end
        subgraph FiatRU["Russian Fiat"]
            YooKassa[YooKassa<br/>Cards + SBP]
            MulenPay[MulenPay<br/>Cards]
            PAL24[PAL24<br/>SBP + Cards]
            Platega[Platega<br/>Multi-method]
            WATA[WATA<br/>Gateway]
            Freekassa[Freekassa<br/>NSPK + Cards]
            CloudPay[CloudPayments<br/>Cards + SBP]
        end
        subgraph Donation["Donations"]
            Tribute[Tribute]
        end
    end

    subgraph TaxCompliance["📋 Tax & Compliance"]
        NaloGO[NaloGO<br/>54-FZ Receipts]
        ReceiptQueue[Receipt Queue]
    end

    subgraph Security["🛡️ Security Services"]
        BanSystem[Ban System API<br/>Cross-bot bans]
        Blacklist[GitHub Blacklist<br/>Fraud prevention]
        DisposableEmail[Email Validator<br/>Fake prevention]
    end

    subgraph DataStorage["💾 Data Storage"]
        PostgreSQL[(PostgreSQL<br/>Primary DB)]
        Redis[(Redis<br/>Cache & Sessions)]
    end

    subgraph TelegramPlatform["📱 Telegram Platform"]
        BotAPI[Telegram Bot Interface<br/>Messages & Callbacks]
        AdminGroup[Admin Group<br/>Forum Topics]
        MiniApp[MiniApp<br/>Web Cabinet]
    end

    %% User connections
    TGUser --> BotAPI
    WebUser --> MiniApp
    AdminUser --> BotAPI
    AdminUser --> MiniApp
    
    %% Bot API to handlers
    BotAPI --> Handlers
    MiniApp --> Services
    
    %% Handlers to services
    Handlers --> Services
    
    %% Services to storage
    Services <--> PostgreSQL
    Services <--> Redis
    
    %% RemnaWave connections
    RemnaWaveService <--> RemnaWaveAPI
    RemnaWaveAPI --> Servers
    RemnaWebhook --> WebhookHandler
    
    %% Payment connections
    PaymentSvc <--> PaymentProviders
    PaymentProviders --> WebhookHandler
    
    %% Tax compliance
    PaymentSvc --> ReceiptQueue
    ReceiptQueue --> NaloGO
    
    %% Security connections
    UserService --> BanSystem
    UserService --> Blacklist
    UserService --> DisposableEmail
    
    %% Notifications
    NotificationService --> AdminGroup
```

## RemnaWave API Detail

```mermaid
flowchart LR
    subgraph BotServices["Bot Services"]
        SubService[Subscription management]
        UserService[User management]
    end
    
    subgraph RemnaWaveAPI["RemnaWave API"]
        direction TB
        
        subgraph Endpoints["API Endpoints"]
            E1["POST /users - Create User"]
            E2["GET /users/uuid - Get User"]
            E3["PATCH /users/uuid - Update User"]
            E4["DELETE /users/uuid - Delete User"]
            E5["GET /squads - List Servers"]
            E6["POST /users/uuid/enable"]
            E7["POST /users/uuid/disable"]
        end
        
        subgraph AuthMethods["Authentication"]
            A1[API Authentication Key]
            A2[Basic Authentication]
            A3[Bearer Authentication Token]
            A4[Cookies + eGames]
            A5[Caddy Authentication Token]
        end
    end
    
    subgraph Webhooks["Incoming Webhooks"]
        W1[user.status.changed]
        W2[subscription.expired]
        W3[subscription.expiring]
        W4[traffic.limited]
        W5[traffic.reset]
        W6[first.connected]
    end
    
    SubService --> Endpoints
    UserService --> Endpoints
    Webhooks --> BotServices
```

## Payment Provider Integration Detail

```mermaid
flowchart TD
    subgraph PaymentFlow["Payment Integration Pattern"]
        direction TB
        
        User([User Initiates Payment]) --> SelectProvider
        SelectProvider{Select Provider}
        
        SelectProvider --> CreateInvoice[Create Invoice/Payment]
        CreateInvoice --> ProviderInterface[Call Provider API]
        ProviderInterface --> GetPaymentLink[Get Payment Link/Invoice]
        GetPaymentLink --> SendToUser[Send to User]
        SendToUser --> UserPays[User Completes Payment]
        UserPays --> WebhookReceived[Payment notification received]
        WebhookReceived --> ValidateSignature{Validate Signature}
        ValidateSignature -->|Invalid| Reject[Reject: 403]
        ValidateSignature -->|Valid| ProcessPayment[Process payment]
        ProcessPayment --> CreditBalance[Credit User Balance]
        CreditBalance --> Success([Success])
    end
```

## Webhook Endpoints Summary

```mermaid
flowchart LR
    subgraph WebhookServer["Webhook Server"]
        direction TB
        W1["yookassa-webhook Port: 8082"]
        W2["cryptobot-webhook Port: 8083"]
        W3["heleket-webhook Port: 8086"]
        W4["tribute-webhook Port: 8081"]
        W5["mulenpay-webhook"]
        W6["pal24-webhook"]
        W7["platega-webhook Port: 8086"]
        W8["wata-webhook"]
        W9["freekassa-webhook"]
        W10["cloudpayments-webhook"]
        W11["remnawave-webhook"]
    end
    
    Providers([Payment Providers]) --> WebhookServer
    RemnaWave([RemnaWave]) --> W11
    WebhookServer --> Handler([Payment Handler])
```

## Security Layer Detail

```mermaid
flowchart TD
    subgraph SecurityChecks["🛡️ Security Checks"]
        direction TB
        
        NewUser([New User Registration]) --> Check1
        
        Check1[Check Ban System API] --> BanResult{Banned?}
        BanResult -->|Yes| Block1[Block User]
        BanResult -->|No| Check2
        
        Check2[Check GitHub Blacklist] --> BlacklistResult{In Blacklist?}
        BlacklistResult -->|Yes| Block2[Block User]
        BlacklistResult -->|No| Check3
        
        Check3[Check Email Domain] --> EmailResult{Disposable?}
        EmailResult -->|Yes| Block3[Block Registration]
        EmailResult -->|No| Check4
        
        Check4[Check Display Name] --> NameResult{Banned Keywords?}
        NameResult -->|Yes| Block4[Restrict Access]
        NameResult -->|No| Allow[Allow]
    end
```

## Data Storage Schema

```mermaid
flowchart LR
    subgraph PostgreSQL["PostgreSQL Tables"]
        direction TB
        Users[(users)]
        Subs[(subscriptions)]
        Trans[(transactions)]
        Tickets[(tickets)]
        Promos[(promo_codes)]
        PromoGroups[(promo_groups)]
        Tariffs[(tariffs)]
        Campaigns[(campaigns)]
        YKPayments[(yookassa_payments)]
        AdminLogs[(admin_logs)]
    end
    
    subgraph Redis["Redis Keys"]
        direction TB
        R1["cart:user ID"]
        R2["state:user ID:chat ID"]
        R3["rate_limit:user ID"]
        R4["notification_cache:id"]
        R5["traffic_snapshot:uuid"]
        R6["session:token"]
        R7["blacklist_cache"]
        R8["warning_sent:user ID"]
    end
```

## Integration Summary Table

| Integration | Type | Direction | Protocol | Auth Method |
|-------------|------|-----------|----------|-------------|
| RemnaWave API | VPN Backend | Bidirectional | REST/HTTPS | API Key/Basic/Cookies |
| RemnaWave Webhooks | VPN Events | Inbound | HTTPS POST | HMAC-SHA256 |
| Telegram Bot API | Messaging | Bidirectional | HTTPS | Bot Token |
| Telegram Stars | Payment | Bidirectional | Built-in | Bot Token |
| CryptoBot | Payment | Bidirectional | REST/HTTPS | API Token |
| Heleket | Payment | Bidirectional | REST/HTTPS | API Key + MD5 |
| YooKassa | Payment | Bidirectional | REST/HTTPS | Shop ID + Secret |
| MulenPay | Payment | Bidirectional | REST/HTTPS | API Key |
| PAL24 | Payment | Bidirectional | REST/HTTPS | API Token |
| Platega | Payment | Bidirectional | REST/HTTPS | Merchant + Secret |
| WATA | Payment | Bidirectional | REST/HTTPS | Access Token |
| Freekassa | Payment | Bidirectional | REST/HTTPS | Secret Words |
| CloudPayments | Payment | Bidirectional | REST/HTTPS | Public ID + Secret |
| Tribute | Donation | Bidirectional | REST/HTTPS | API Key |
| NaloGO | Tax | Outbound | REST/HTTPS | INN + Password |
| Ban System | Security | Bidirectional | REST/HTTPS | Bearer Token |
| GitHub Blacklist | Security | Inbound | HTTPS GET | None |
| PostgreSQL | Storage | Bidirectional | PostgreSQL | Connection String |
| Redis | Cache | Bidirectional | Redis Protocol | Password |
| Disposable Email | Fake registration prevention | `DISPOSABLE_EMAIL_CHECK_ENABLED` |

### 💾 Storage

| Service | Purpose |
|---------|---------|
| PostgreSQL | Primary database |
| Redis | Caching, sessions, queues |

---

**Related Diagrams:**
- [System Architecture](./01-system-architecture.md)
- [Payment Processing](./06-payment-processing.md)
- [Data Flow](./12-data-flow.md)
