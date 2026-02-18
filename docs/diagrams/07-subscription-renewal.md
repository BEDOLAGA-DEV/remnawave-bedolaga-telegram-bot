# 🔄 Subscription Renewal & Auto-Pay Flow

> Automatic and manual subscription renewal processes with expiry warnings and balance checks.

## Overview

The renewal system ensures continuous service through automated renewals (auto-pay) and timely expiry warnings. It handles both scheduled background checks and user-initiated manual renewals.

## Main Flow Diagram

```mermaid
flowchart TD
    Scheduler([Scheduler Runs Every MONITORING_INTERVAL]) --> LoadConfig[Load Renewal Settings]
    LoadConfig --> QuerySubs[Find active subscriptions]
    
    QuerySubs --> FilterExpiring[Filter: Expiring Within Warning Period]
    FilterExpiring --> ForEachSubscription{Process Each Subscription}
    
    ForEachSubscription --> LoadUser[Get user information]
    LoadUser --> LoadSubDetails[Load Subscription Details]
    LoadSubDetails --> CalculateDaysRemaining[Calculate Days Until Expiry]
    
    CalculateDaysRemaining --> CheckExpired{Already Expired?}
    CheckExpired -->|Yes| HandleExpired[Handle Expired Subscription]
    CheckExpired -->|No| CheckAutomaticPayment{Auto-Pay Enabled?}
    
    HandleExpired --> CheckDeleteMode{REMNAWAVE_USER_DELETE_MODE}
    CheckDeleteMode -->|delete| DeleteRemnaUser[Delete User in RemnaWave]
    CheckDeleteMode -->|disable| DisableRemnaUser[Disable User in RemnaWave]
    DeleteRemnaUser --> UpdateSubscriptionExpired[Update Subscription: Expired]
    DisableRemnaUser --> UpdateSubscriptionExpired
    UpdateSubscriptionExpired --> NotifyExpired[Notify User: Subscription Expired]
    NotifyExpired --> NextSubscription
    
    CheckAutomaticPayment -->|Yes| AutomaticPaymentFlow
    CheckAutomaticPayment -->|No| ManualFlow
    
    subgraph AutomaticPaymentFlow["🤖 Auto-Pay Flow"]
        AP1[Check Auto-Pay Days Before] --> AP2{Within Auto-Pay Window?}
        AP2 -->|No| APSkip[Skip - Not Yet]
        AP2 -->|Yes| AP3[Calculate Renewal Price]
        AP3 --> AP4[Load User Balance]
        AP4 --> AP5{Balance >= Price?}
        AP5 -->|No| AP6[Send Low Balance Warning]
        AP6 --> AP7[Suggest Top-up Amount]
        AP7 --> APSkip
        AP5 -->|Yes| AP8[Process Auto-Renewal]
    end
    
    subgraph ManualFlow["👤 Manual Renewal Check"]
        MF1[Check Warning Days Config] --> MF2{In Warning Period?}
        MF2 -->|No| MFSkip[Skip - Too Early]
        MF2 -->|Yes| MF3{Warning Already Sent?}
        MF3 -->|Yes| MFSkip
        MF3 -->|No| MF4[Send Expiry Warning]
        MF4 --> MF5[Cache warning sent]
    end
    
    AP8 --> ProcessRenewal
    
    subgraph ProcessRenewal["🔄 Process Renewal"]
        PR1[Process purchase] --> PR2[Subtract from balance]
        PR2 --> PR3[Calculate New Expiry Date]
        PR3 --> PR4{Reset Traffic?}
        PR4 -->|Yes| PR5[Reset Traffic Counter]
        PR4 -->|No| PR6[Keep Traffic]
        PR5 --> PR7[Update RemnaWave]
        PR6 --> PR7
        PR7 --> PR8{RemnaWave Success?}
        PR8 -->|No| PR9[Cancel and refund]
        PR9 --> PR10[Record error]
        PR10 --> RenewalFailed[Renewal Failed]
        PR8 -->|Yes| PR11[Update subscription]
        PR11 --> PR12[Create transaction record]
        PR12 --> PR13[Complete operation]
        PR13 --> RenewalSuccess[Renewal Success]
    end
    
    RenewalSuccess --> NotifyRenewal[Notify User: Renewed]
    NotifyRenewal --> NotifyAdministratorRenewal[Notify Admin Group]
    NotifyAdministratorRenewal --> NextSubscription
    
    RenewalFailed --> NotifyAdministratorError[Alert Admin: Renewal Failed]
    NotifyAdministratorError --> NextSubscription
    
    APSkip --> NextSubscription
    MFSkip --> NextSubscription
    MF5 --> NextSubscription
    
    NextSubscription([Next Subscription]) --> ForEachSubscription
    ForEachSubscription -->|All Processed| SchedulerComplete([Scheduler Complete])

    style Scheduler fill:#FF9800
    style SchedulerComplete fill:#2196F3
    style RenewalFailed fill:#f44336
```

## Manual Renewal User Flow

```mermaid
flowchart TD
    User([User]) --> ViewSubscription[View My Subscription]
    ViewSubscription --> ShowStatus[Show Subscription Status]
    
    ShowStatus --> CheckExpiring{Expiring Soon?}
    CheckExpiring -->|Yes| ShowRenewButton[Show Renew Button]
    CheckExpiring -->|No| ShowNormal[Show Normal Status]
    
    ShowRenewButton --> ClickRenew[User Clicks Renew]
    ClickRenew --> SelectPeriod[Select Renewal Period]
    
    SelectPeriod --> ShowPeriods[Show Available Periods]
    ShowPeriods --> UserSelectsPeriod[User Selects Period]
    
    UserSelectsPeriod --> CalculatePrice[Calculate Renewal Price]
    CalculatePrice --> ApplyDiscounts[Apply Promo Group Discounts]
    ApplyDiscounts --> ShowPrice[Show Final Price]
    
    ShowPrice --> CheckBalanceance{Balance >= Price?}
    CheckBalanceance -->|No| OfferTopUp[Offer Top-up Options]
    OfferTopUp --> SaveToCart[Save Renewal to Cart]
    SaveToCart --> TopupFlow[Top-up Flow]
    TopupFlow --> AutoProcess{Auto-Process?}
    AutoProcess -->|Yes| ProcessRenewal
    AutoProcess -->|No| ReturnToMenu[Return to Menu]
    
    CheckBalanceance -->|Yes| ShowConfirm[Show Confirmation]
    ShowConfirm --> UserConfirms[User Confirms]
    
    UserConfirms --> ProcessRenewal[Process Renewal]
    ProcessRenewal --> DeductBalance[Subtract from balance]
    DeductBalance --> ExtendInRemna[Extend VPN subscription]
    ExtendInRemna --> UpdateDatabase[Update Subscription]
    UpdateDatabase --> CreateTransactionaction[Create Transaction]
    CreateTransactionaction --> ShowSuccess[Show Success Message]
    ShowSuccess --> ShowNewExpiry[Show New Expiry Date]
    ShowNewExpiry --> Done([Renewal Complete])

    style User fill:#4CAF50
    style Done fill:#2196F3
```

## Warning Schedule Timeline

```mermaid
gantt
    title Subscription Renewal Timeline
    dateFormat YYYY-MM-DD
    axisFormat %d
    
    section Subscription
    Active Period           :active, sub, 2024-01-01, 30d
    
    section Warnings
    Warning 1 (7 days)      :crit, w1, 2024-01-24, 1d
    Warning 2 (3 days)      :crit, w2, 2024-01-28, 1d
    Warning 3 (1 day)       :crit, w3, 2024-01-30, 1d
    
    section Auto-Pay
    Auto-Pay Window         :done, ap, 2024-01-28, 3d
    
    section Expiry
    Expiry Date             :milestone, exp, 2024-01-31, 0d
```

## Expiry Warning Message Example

```mermaid
flowchart LR
    subgraph WarningMessage["⚠️ Expiry Warning Message"]
        direction TB
        Title["⚠️ Your subscription expires soon!"]
        Expiry["📅 Expiry: January 31, 2024"]
        Remaining["⏰ Remaining: 3 days"]
        Price["💰 Renewal price: 499 ₽"]
        Buttons["[Renew Now] [Top-up]"]
    end
```

## State Machine

```mermaid
stateDiagram-v2
    [*] --> Active: Subscription Created
    
    Active --> WarningPeriod: Days Left < Warning Days
    WarningPeriod --> Active: User Renews
    
    WarningPeriod --> AutomaticPaymentWindow: Days Left < AutomaticPayment Days
    AutomaticPaymentWindow --> Active: Auto-Renewal Success
    AutomaticPaymentWindow --> LowBalance: Insufficient Funds
    LowBalance --> AutomaticPaymentWindow: User Tops Up
    LowBalance --> Expired: No Action Before Expiry
    
    AutomaticPaymentWindow --> Expired: No renewal
    WarningPeriod --> Expired: No Action Before Expiry
    
    Expired --> Disabled: REMNAWAVE_USER_DELETE_MODE=disable
    Expired --> Deleted: REMNAWAVE_USER_DELETE_MODE=delete
    
    Disabled --> Active: User Reactivates
    Deleted --> [*]
    
    Active --> Active: User Manually Renews Early
```

## Sequence Diagram

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant DB as PostgreSQL
    participant RW as RemnaWave
    participant U as User
    participant A as Admin Group
    
    S->>DB: Query subscriptions expiring in 3 days
    DB-->>S: List of subscriptions
    
    loop For each subscription
        S->>DB: Load user data
        S->>DB: Check automatic payment setting
        
        alt Auto-Pay Enabled
            S->>DB: Check user balance
            alt Sufficient Balance
                S->>DB: BEGIN TRANSACTION
                S->>DB: Deduct balance
                S->>RW: Extend subscription
                RW-->>S: Success
                S->>DB: Update subscription
                S->>DB: Create transaction
                S->>DB: COMMIT
                S->>U: Send renewal confirmation
                S->>A: Log: Auto-renewal success
            else Insufficient Balance
                S->>U: Send low balance warning
                S->>A: Log: Low balance warning sent
            end
        else Manual Mode
            S->>DB: Check warning sent cache
            alt Not Sent Yet
                S->>U: Send expiry warning
                S->>DB: Cache warning sent
            end
        end
    end
```
