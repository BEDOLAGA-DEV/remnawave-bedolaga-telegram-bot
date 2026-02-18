# 👨‍💼 Admin Management Flow

> Administrative panel for managing users, subscriptions, payments, promotions, and system configuration.

## Overview

The admin panel provides comprehensive tools for managing all aspects of the VPN bot business. Accessible only to users listed in `ADMIN_IDS`, it covers user management, financial operations, promotional campaigns, and system maintenance.

## Main Panel Structure

```mermaid
flowchart TD
    Admin([Admin User]) --> CheckAdmin{Verify Admin ID}
    CheckAdmin -->|Not Admin| AccessDenied[Access Denied]
    CheckAdmin -->|Admin| AdminPanel[Admin Panel]
    
    AdminPanel --> MainMenu{Admin Main Menu}
    
    MainMenu --> UserMgmt[User Management]
    MainMenu --> SubMgmt[Subscription Management]
    MainMenu --> PaymentMgmt[Payment Management]
    MainMenu --> PromoMgmt[Promo and Campaigns]
    MainMenu --> SystemMgmt[System Settings]
    MainMenu --> Reports[Reports and Analytics]
    MainMenu --> Support[Support Management]
    MainMenu --> Broadcast[Broadcast]
    
    subgraph UserMgmt["👥 User Management"]
        U1[Search Users]
        U2[View User Profile]
        U3[Block/Unblock User]
        U4[Modify Balance]
        U5[View Transaction History]
        U6[Manage Blacklist]
        U7[Export User Data]
    end
    
    subgraph SubMgmt["📋 Subscription Management"]
        S1[View All Subscriptions]
        S2[Search by Status]
        S3[Extend Subscription]
        S4[Revoke Subscription]
        S5[Sync with RemnaWave]
        S6[Modem Subscriptions]
        S7[Bulk Operations]
    end
    
    subgraph PaymentMgmt["💰 Payment Management"]
        P1[View Transactions]
        P2[Manual Top-up]
        P3[Process Refund]
        P4[Payment Verification]
        P5[Failed Payments]
        P6[Export Transactions]
    end
    
    subgraph PromoMgmt["🎁 Promo and Campaigns"]
        PR1[Create Promo Code]
        PR2[Manage Promo Groups]
        PR3[Create Campaign]
        PR4[Manage Tariffs]
        PR5[Promo Offers]
        PR6[View Usage Stats]
    end
    
    subgraph SystemMgmt["⚙️ System Settings"]
        SY1[Maintenance Mode]
        SY2[Database Backup]
        SY3[Server Status]
        SY4[Localization]
        SY5[Welcome Text]
        SY6[Rules & Privacy]
        SY7[System Logs]
    end
    
    subgraph Reports["📊 Reports and Analytics"]
        R1[Daily Summary]
        R2[Revenue Analytics]
        R3[User Statistics]
        R4[Referral Analytics]
        R5[Conversion Funnel]
        R6[Custom Reports]
    end

    style Admin fill:#9C27B0
    style Reports fill:#2196F3
```

## User Management Detail

```mermaid
flowchart TD
    UserMgmt([User Management]) --> SearchUser[Search User]
    
    SearchUser --> SearchBy{Search By}
    SearchBy -->|Telegram ID| SearchTgId[Enter Telegram ID]
    SearchBy -->|Username| SearchUsername[Enter Username]
    SearchBy -->|Email| SearchEmail[Enter Email]
    SearchBy -->|Phone| SearchPhone[Enter Phone]
    
    SearchTgId --> FindUser[Find User]
    SearchUsername --> FindUser
    SearchEmail --> FindUser
    SearchPhone --> FindUser
    
    FindUser --> UserFound{User Found?}
    UserFound -->|No| ShowNotFound[Show: User Not Found]
    UserFound -->|Yes| ShowProfile[Show User Profile]
    
    ShowProfile --> ProfileActions{Available Actions}
    
    ProfileActions -->|View Details| ShowDetails
    subgraph ShowDetails["📋 User Details"]
        D1[Telegram Info]
        D2[Registration Date]
        D3[Balance: Main + Referral]
        D4[Subscription Status]
        D5[Transaction Count]
        D6[Referral Stats]
        D7[Last Activity]
    end
    
    ProfileActions -->|Modify Balance| ModifyBalance
    subgraph ModifyBalance["💰 Balance Modification"]
        MB1[Select Action: Add/Subtract/Set]
        MB2[Enter Amount]
        MB3[Enter Reason]
        MB4[Confirm Change]
        MB5[Create Admin Transaction]
        MB6[Record admin activity]
    end
    
    ProfileActions -->|Block User| BlockUser
    subgraph BlockUser["🚫 Block User"]
        BU1[Select Block Type]
        BU2[Set Duration: Temp/Permanent]
        BU3[Enter Reason]
        BU4[Confirm Block]
        BU5[Disable Subscription]
        BU6[Notify User]
    end
    
    ProfileActions -->|View History| ViewHistory[Show Transaction History]
```

## Promo Code Management

```mermaid
flowchart TD
    PromoMgmt([Promo Management]) --> PromoAction{Action}
    
    PromoAction -->|Create| CreatePromo
    PromoAction -->|List| ListPromos[List Active Promos]
    PromoAction -->|Edit| EditPromo[Edit Promo]
    PromoAction -->|Delete| DeletePromo[Delete Promo]
    
    subgraph CreatePromo["🎁 Create Promo Code"]
        CP1[Enter Code or Auto-Generate]
        CP2[Select Type]
        CP3[Set Value]
        CP4[Set Limits]
        CP5[Set Validity]
        CP6[Set Restrictions]
        CP7[Save Promo]
    end
    
    CP2 --> PromoTypes{Promotion Types}
    PromoTypes -->|Percentage| TypePercent[Percentage Discount]
    PromoTypes -->|Fixed| TypeFixed[Fixed Amount Off]
    PromoTypes -->|Days| TypeDays[Additional Days]
    PromoTypes -->|Balance| TypeBalance[Add to Balance]
    PromoTypes -->|Trial| TypeTrial[Extended Trial]
    
    CP4 --> Limits{Limits}
    Limits --> MaxUses[Max Total Uses]
    Limits --> PerUser[Uses Per User]
    
    CP6 --> Restrictions{Restrictions}
    Restrictions --> MinPurchase[Min Purchase Amount]
    Restrictions --> TariffRestrict[Allowed Tariffs]
    Restrictions --> UserRestrict[Allowed User Groups]
```

## Database Backup Flow

```mermaid
flowchart TD
    Backup([Database Backup]) --> BackupAction{Action}
    
    BackupAction -->|Create| CreateBackup
    BackupAction -->|List| ListBackups[List Available Backups]
    BackupAction -->|Restore| RestoreBackup
    BackupAction -->|Download| DownloadBackup
    
    subgraph CreateBackup["💾 Create Backup"]
        CB1[Start Backup Job]
        CB2[Dump PostgreSQL]
        CB3[Compress File]
        CB4[Generate Filename with Timestamp]
        CB5[Store Backup]
        CB6[Notify Admin: Backup Complete]
    end
    
    subgraph RestoreBackup["🔄 Restore Backup"]
        RB1[Select Backup File]
        RB2[Show Warning]
        RB3[Require Confirmation]
        RB4[Enable Maintenance Mode]
        RB5[Restore Database]
        RB6[Verify Integrity]
        RB7[Disable Maintenance Mode]
        RB8[Notify Admin: Restore Complete]
    end
```

## Daily Report Content

```mermaid
flowchart LR
    subgraph DailyReport["📊 Daily Report"]
        direction TB
        H1["📅 Date: February 17, 2026"]
        H2["👥 New Users: 45"]
        H3["💰 Revenue: 125,000₽"]
        H4["📋 New Subscriptions: 28"]
        H5["🔄 Renewals: 15"]
        H6["🎁 Trials Activated: 32"]
        H7["📈 Trial → Paid: 18%"]
        H8["👥 Active Referrals: 12"]
        H9["💸 Referral Payouts: 8,500₽"]
    end
```

## Admin Notification Topics

```mermaid
flowchart TD
    AdminGroup([Admin Telegram Group]) --> Topics{Forum Topics}
    
    Topics --> Topic1[Payments]
    Topics --> Topic2[Subscriptions]
    Topics --> Topic3[Support Tickets]
    Topics --> Topic4[System Alerts]
    Topics --> Topic5[Errors]
    Topics --> Topic6[New Users]
    Topics --> Topic7[Reports]
    Topics --> Topic8[Referral Withdrawals]
    
    Topic1 --> PaymentEvents[New payments, refunds]
    Topic2 --> SubEvents[Purchases, renewals, expirations]
    Topic3 --> TicketEvents[New tickets, SLA breaches]
    Topic4 --> SystemEvents[Maintenance, backups, sync]
    Topic5 --> ErrorEvents[API failures, exceptions]
```

## Sequence Diagram: Manual Top-up

```mermaid
sequenceDiagram
    participant A as Admin
    participant B as Bot
    participant DB as PostgreSQL
    participant U as User
    participant N as Notification
    
    A->>B: Search user by ID
    B->>DB: Find user
    DB-->>B: User found
    B->>A: Show user profile
    A->>B: Click "Modify Balance"
    B->>A: Show balance form
    A->>B: Add 500₽, Reason: "Compensation"
    B->>DB: BEGIN TRANSACTION
    B->>DB: Update user balance
    B->>DB: Create admin_transaction record
    B->>DB: COMMIT
    B->>A: Show success message
    B->>U: Notify: Balance updated +500₽
    B->>N: Log admin action
```

**Related Diagrams:**
- [System Architecture](./01-system-architecture.md)
- [Support Ticket System](./10-support-ticket.md)
