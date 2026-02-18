# 📊 Data Flow Diagram

> Comprehensive view of how data moves through the system from input sources to output destinations.

## Overview

This diagram illustrates the complete data flow within the Remnawave Bedolaga Bot, showing how different input sources are processed through multiple layers and result in various outputs.

## Complete Data Flow

```mermaid
flowchart TD
    subgraph InputLayer["📥 Input Layer"]
        subgraph TelegramInput["Telegram Input"]
            Commands[Commands: start help menu]
            TextMsg[Text Messages]
            Callbacks[Callback Queries]
            PreCheckout[Pre-checkout Queries]
            SuccessfulPay[Successful Payments]
        end
        
        subgraph WebhookInput["Webhook Input"]
            PaymentWH[Payment Webhooks]
            RemnaWH[RemnaWave Webhooks]
        end
        
        subgraph ScheduledInput["Scheduled Input"]
            RenewalCheck[Renewal Checker]
            ServiceLevelCheck[Service Level Monitor]
            BackupJob[Backup Jobs]
            SyncJob[RemnaWave Sync]
            TrafficCheck[Traffic Monitor]
        end
        
        subgraph AdminInput["Admin Input"]
            AdminCommands[Admin Commands]
            MiniAppAPI[Web Application Interface Calls]
        end
    end

    subgraph MiddlewareLayer["🔄 Middleware Layer"]
        direction LR
        MW1[Auth] --> MW2[Rate Limit]
        MW2 --> MW3[Blacklist]
        MW3 --> MW4[Maintenance]
        MW4 --> MW5[Channel Check]
        MW5 --> MW6[Display Name]
        MW6 --> MW7[Context Bind]
        MW7 --> MW8[Handle errors]
        MW8 --> MW9[Logging]
    end

    subgraph ProcessingLayer["⚙️ Processing Layer"]
        subgraph Handlers["Handlers"]
            StartHandler[Start Handler]
            MenuHandler[Menu processing]
            SubscriptionHandler[Subscription Handler]
            PaymentHandler[Payment Handler]
            ReferralHandler[Referral Handler]
            SupportHandler[Support Handler]
            AdminHandler[Admin commands processing]
        end
        
        subgraph Services["Services"]
            UserService[User management]
            SubscriptionService[Subscription management]
            PaymentService[Payment processing]
            RemnaWaveService[VPN management]
            PromotionService[Promocode Service]
            ReferralService[Referral program]
            CartService[Shopping cart]
            TaxReceiptService[Tax receipt system]
        end
        
        subgraph Validators["Validators"]
            PromotionValidator[Promotion Code Validation]
            PayVal[Payment Validation]
            InputVal[Input Validation]
            SigVal[Signature Validation]
        end
    end

    subgraph StorageLayer["💾 Storage Layer"]
        subgraph PostgreSQL["PostgreSQL"]
            Users[(users)]
            Subs[(subscriptions)]
            Trans[(transactions)]
            Tickets[(tickets)]
            Promos[(promo_codes)]
            Tariffs[(tariffs)]
            Campaigns[(campaigns)]
        end
        
        subgraph Redis["Redis"]
            Cart[cart:*]
            FSM[state:*]
            Rate[rate:*]
            Cache[cache:*]
            Session[session:*]
        end
    end

    subgraph OutputLayer["📤 Output Layer"]
        subgraph UserOutput["User Output"]
            UserMsg[Telegram Messages]
            UserMedia[Media: QR, Images]
            UserKeyboard[Inline Keyboards]
        end
        
        subgraph AdminOutput["Admin Output"]
            AdminNotif[Admin Notifications]
            Reports[Reports]
            Logs[System Logs]
        end
        
        subgraph ExternalOutput["External Output"]
            RemnaWaveAPI[RemnaWave API Calls]
            PaymentAPI[Payment API Calls]
            TaxServiceInterface[NaloGO API Calls]
        end
    end

    %% Input to Middleware
    TelegramInput --> MiddlewareLayer
    WebhookInput --> ProcessingLayer
    ScheduledInput --> Services
    AdminInput --> MiddlewareLayer
    
    %% Middleware to Processing
    MiddlewareLayer --> Handlers
    
    %% Handlers to Services
    Handlers --> Validators
    Validators --> Services
    
    %% Services to Storage
    Services <--> PostgreSQL
    Services <--> Redis
    
    %% Services to Output
    Services --> UserOutput
    Services --> AdminOutput
    Services --> ExternalOutput
```

## Request Processing Flow

```mermaid
sequenceDiagram
    participant U as User
    participant TG as Telegram API
    participant B as Bot (Polling/Webhook)
    participant MW as Middleware Chain
    participant R as Router
    participant H as Handler
    participant S as Service
    participant DB as PostgreSQL
    participant RD as Redis
    participant EXT as External API
    
    U->>TG: Send Message/Callback
    TG->>B: Deliver Update
    B->>MW: Process through middlewares
    
    Note over MW: Auth → Rate Limit → Blacklist → ...
    
    MW->>MW: Load/Create User
    MW->>RD: Get/Set FSM State
    MW->>R: Route to Handler
    R->>H: Execute Handler
    
    H->>S: Call Business Logic
    S->>DB: Read/Write Data
    DB-->>S: Data Result
    S->>RD: Cache Operations
    RD-->>S: Cache Result
    
    alt External API Needed
        S->>EXT: API Call
        EXT-->>S: API Response
    end
    
    S-->>H: Business Result
    H-->>B: Response (Message/Edit)
    B->>TG: Send Response
    TG->>U: Deliver Response
```

## Middleware Processing Detail

```mermaid
flowchart TD
    Request([Incoming Request]) --> ExtractUpdate[Read message details]
    ExtractUpdate --> GetUser[Get User from Update]
    
    GetUser --> AuthMiddleware{Auth Middleware}
    AuthMiddleware --> CheckDatabase[Check if user exists]
    CheckDatabase --> UserExists{User Exists?}
    UserExists -->|No| CreateUser[Create User Record]
    UserExists -->|Yes| LoadUser[Get user information]
    CreateUser --> InjectUser
    LoadUser --> InjectUser[Pass user info to processing]
    
    InjectUser --> RateMW{Rate Limit}
    RateMW --> CheckRate[Check Redis Counter]
    CheckRate --> RateOK{Within Limit?}
    RateOK -->|No| RateReject[Rate Limited]
    RateOK -->|Yes| IncrementCounter[Increment Counter]
    
    IncrementCounter --> BlacklistMiddleware{Blacklist Check}
    BlacklistMiddleware --> CheckBlacklist[Check Ban Status]
    CheckBlacklist --> NotBanned{Not Banned?}
    NotBanned -->|No| BanReject[User Blocked]
    NotBanned -->|Yes| MaintenanceMiddleware
    
    MaintenanceMiddleware{Maintenance Check} --> IsMaintenanceMode{Maintenance Mode?}
    IsMaintenanceMode -->|Yes, not Admin| MaintenanceMessage[Show Maintenance Message]
    IsMaintenanceMode -->|No or Admin| ChannelMiddleware
    
    ChannelMiddleware{Channel Check} --> ChannelRequired{Required?}
    ChannelRequired -->|Yes| VerifyChannel[Verify Subscription]
    ChannelRequired -->|No| ContextMiddleware
    VerifyChannel --> Subscribed{Subscribed?}
    Subscribed -->|No| ChannelPrompt[Show Subscribe Prompt]
    Subscribed -->|Yes| ContextMiddleware
    
    ContextMiddleware[Context Binding] --> BindDatabase[Bind DB Session]
    BindDatabase --> BindUser[Bind User Object]
    BindUser --> LoggingMiddleware
    
    LoggingMiddleware[Logging] --> LogRequest[Log Request Details]
    LogRequest --> Handler([Process request])

    style Request fill:#4CAF50
    style Handler fill:#2196F3
    style RateReject fill:#f44336
    style BanReject fill:#f44336
```

## Data Transformation Pipeline

```mermaid
flowchart LR
    subgraph Input["Raw Input"]
        RawJSON[JSON Payload]
        RawCallback[Callback Data]
        RawCommand[Command Text]
    end
    
    subgraph Parse["Parse & Validate"]
        ParseJSON[Parse JSON]
        ParseCallback[Parse Callback]
        ParseCommand[Parse Command]
        Validate[Validate Schema]
    end
    
    subgraph Transform["Transform"]
        ToDataObject[Convert to DTO]
        Sanitize[Sanitize Input]
        Normalize[Normalize Data]
    end
    
    subgraph Process["Process"]
        BusinessLogic[Apply Business Logic]
        DBOperations[Save data]
        CacheOps[Cache data]
    end
    
    subgraph Output["Format Output"]
        FormatMsg[Format Message]
        BuildKeyboard[Build Keyboard]
        GenerateMedia[Generate Media]
    end
    
    Input --> Parse --> Transform --> Process --> Output
```

## Caching Strategy

```mermaid
flowchart TD
    Request([Data Request]) --> CheckCache{Check Redis Cache}
    
    CheckCache -->|Cache Hit| ReturnCached[Return Cached Data]
    CheckCache -->|Cache Miss| QueryDatabase[Query PostgreSQL]
    
    QueryDatabase --> StoreCache[Store in cache]
    StoreCache --> SetTTL[Set TTL]
    SetTTL --> ReturnFresh[Return Fresh Data]
    
    subgraph CacheKeys["Cache Key Patterns"]
        K1["user:{id} → User profile (TTL: 5min)"]
        K2["subscription:{user ID} → Sub details (TTL: 1min)"]
        K3["tariffs:active → Active tariffs (TTL: 10min)"]
        K4["servers:list → Server list (TTL: 5min)"]
        K5["promo:{code} → Promo details (TTL: 1min)"]
    end
    
    subgraph Invalidation["Cache Invalidation"]
        I1["User Update: Delete user:*"]
        I2["Subscription Change: Delete subscription:*"]
        I3["Admin Promo Edit: Delete promo:*"]
        I4["Server Sync: Delete servers:*"]
    end
```

## Error Handling Flow

```mermaid
flowchart TD
    Error([Error Occurs]) --> CatchError[Catch in Error Handler]
    CatchError --> ClassifyError{Classify Error Type}
    
    ClassifyError -->|Validation| ValidationError[Return Validation Message]
    ClassifyError -->|Business Logic| BusinessError[Return User-Friendly Message]
    ClassifyError -->|External API| APIError[Log + Retry/Fallback]
    ClassifyError -->|Database| DBError[Log + Show Generic Error]
    ClassifyError -->|Unknown| UnknownError[Log Full Stack]
    
    ValidationError --> UserResponse[Send to User]
    BusinessError --> UserResponse
    
    APIError --> RetryLogic{Retry?}
    RetryLogic -->|Yes| RetryRequest[Retry with Backoff]
    RetryLogic -->|No| Fallback[Use Fallback]
    RetryRequest --> Success{Success?}
    Success -->|Yes| Continue[Continue Flow]
    Success -->|No| AdminAlert
    Fallback --> Continue
    
    DBError --> AdminAlert[Alert Admin]
    UnknownError --> AdminAlert
    AdminAlert --> LogToTopic[Log to Error Topic]
```
|-------------|---------|-----|
| `cart:{user ID}` | Shopping cart | 1 hour |
| `state:{user ID}` | FSM state | Session |
| `rate:{user ID}` | Rate limiting | Variable |

## Output Destinations

| Output | Purpose |
|--------|---------|
| User Notifications | Payment confirmations, warnings |
| Admin Notifications | System alerts, new tickets |
| External API Calls | RemnaWave, payments |
| Reports | Daily/weekly analytics |

---

**Related Diagrams:**
- [System Architecture](./01-system-architecture.md)
- [External Integrations](./11-external-integrations.md)
