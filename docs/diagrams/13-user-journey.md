# 🗺️ Complete User Journey

> End-to-end visualization of the user experience from discovery to active usage and referral.

## Overview

This diagram shows the complete journey a user takes through the VPN bot service, from first discovering the bot through becoming an active, referring customer.

## Journey Diagram

```mermaid
journey
    title Complete VPN User Journey
    section Discovery
      Find Bot: 3: User
      Click Start: 5: User
    section Onboarding
      Accept Privacy: 4: User
      Accept Rules: 4: User
      Select Language: 5: User
    section Trial
      Activate Trial: 5: User
      Get VPN Config: 5: Bot
      Test Connection: 4: User
    section Conversion
      Trial Expires: 2: Bot
      Receive Warning: 3: Bot
      Top-up Balance: 4: User
      Purchase Subscription: 5: User
    section Active User
      Use VPN Daily: 5: User
      Monitor Traffic: 3: Bot
      Receive Notifications: 4: Bot
    section Renewal
      Expiry Warning: 3: Bot
      Manual/Auto Renew: 5: User, Bot
    section Referral
      Share Link: 4: User
      Earn Commission: 5: Bot
```

## Complete Lifecycle Flow

```mermaid
flowchart TD
    subgraph Discovery["1️⃣ Discovery"]
        D1[Find Bot] --> D2[Click Link]
        D2 --> D3{Source?}
        D3 -->|Organic| D4[Direct Search]
        D3 -->|Referral| D5[Referral Link]
        D3 -->|Campaign| D6[Ad Campaign]
        D3 -->|Social| D7[Social Media]
    end
    
    subgraph Onboarding["2️⃣ Onboarding"]
        O1[Start Command] --> O2{New User?}
        O2 -->|Yes| O3[Channel Subscription]
        O2 -->|No| O8[Welcome Back]
        O3 --> O4[Privacy Policy]
        O4 --> O5[Service Rules]
        O5 --> O6[Language Selection]
        O6 --> O7[Main Menu]
        O8 --> O7
    end
    
    subgraph Trial["3️⃣ Trial Phase"]
        T1[View Trial Option] --> T2{Eligible?}
        T2 -->|No| T3[Show Purchase]
        T2 -->|Yes| T4[Activate Trial]
        T4 --> T5[Create VPN Account]
        T5 --> T6[Receive Config]
        T6 --> T7[First Connection]
        T7 --> T8[Use VPN]
        T8 --> T9{Trial Ending?}
        T9 -->|No| T8
        T9 -->|Yes| T10[Expiry Warning]
    end
    
    subgraph Conversion["4️⃣ Conversion"]
        C1[Trial Expired] --> C2{User Action?}
        C2 -->|Purchase| C3[Select Plan]
        C2 -->|Ignore| C4[Lost User]
        C3 --> C5[Choose Payment]
        C5 --> C6[Complete Payment]
        C6 --> C7[Subscription Active]
        C4 --> C8[Re-engagement Campaign]
        C8 -->|Returns| C3
    end
    
    subgraph Active["5️⃣ Active User"]
        A1[Daily VPN Usage] --> A2[Traffic Monitoring]
        A2 --> A3{Usage Alerts?}
        A3 -->|High Traffic| A4[Usage Warning]
        A3 -->|Normal| A1
        A4 --> A5[Offer Traffic Top-up]
        A5 --> A1
    end
    
    subgraph Renewal["6️⃣ Renewal"]
        R1[Expiry Approaching] --> R2{Auto-Pay?}
        R2 -->|Yes| R3{Balance OK?}
        R3 -->|Yes| R4[Auto-Renew]
        R3 -->|No| R5[Low Balance Warning]
        R2 -->|No| R6[Manual Renewal Prompt]
        R5 --> R7[User Tops Up]
        R7 --> R4
        R6 --> R8{User Renews?}
        R8 -->|Yes| R4
        R8 -->|No| R9[Subscription Expires]
        R4 --> A1
    end
    
    subgraph Referral["7️⃣ Referral"]
        RF1[Access Referral Menu] --> RF2[Get Referral Link]
        RF2 --> RF3[Share with Friends]
        RF3 --> RF4[Friend Registers]
        RF4 --> RF5[Friend Tops Up]
        RF5 --> RF6[Earn Commission]
        RF6 --> RF7[Withdraw/Use Balance]
    end
    
    D4 --> O1
    D5 --> O1
    D6 --> O1
    D7 --> O1
    
    O7 --> T1
    T3 --> C3
    T10 --> C1
    
    C7 --> A1
    R9 --> C8
    
    A1 -.-> RF1

    style Discovery fill:#E3F2FD
    style Onboarding fill:#E8F5E9
    style Trial fill:#FFF3E0
    style Conversion fill:#FCE4EC
    style Active fill:#E0F7FA
    style Renewal fill:#F3E5F5
    style Referral fill:#FFF8E1
```

## User State Machine

```mermaid
stateDiagram-v2
    [*] --> Visitor: Discovers Bot
    
    Visitor --> Registered: Completes Onboarding
    Visitor --> Bounced: Leaves Before Registration
    
    Registered --> TrialUser: Activates Trial
    Registered --> DirectPurchase: Purchases Immediately
    
    TrialUser --> TrialActive: Trial Running
    TrialActive --> TrialExpiring: Days Remaining < 2
    TrialExpiring --> TrialExpired: Trial Ends
    TrialActive --> Converted: Early Purchase
    
    TrialExpired --> Converted: Purchases Within Grace
    TrialExpired --> Churned: No Action
    
    DirectPurchase --> PaidActive
    Converted --> PaidActive
    
    PaidActive --> Renewing: Expiry Approaching
    Renewing --> PaidActive: Renewed
    Renewing --> Expired: Not Renewed
    
    Expired --> Reactivated: Returns and Purchases
    Expired --> Churned: Inactive > 30 days
    
    Reactivated --> PaidActive
    
    PaidActive --> Referrer: Shares Link
    Referrer --> PaidActive: Continues Usage
    
    Churned --> Reactivated: Win-back Campaign
    Bounced --> [*]
```

## Journey Phases

### 1️⃣ Discovery Phase

**How users find the bot:**

| Source | Description | Tracking | Conversion |
|--------|-------------|----------|------------|
| 🔍 Organic Search | Direct bot search in Telegram | None | ~15% |
| 👥 Referral Link | Friend's referral link | Referral code | ~35% |
| 📢 Ad Campaign | Marketing deep link | Campaign ID | ~20% |
| 📱 Social Media | Links in posts | UTM parameters | ~25% |

**Entry Points:**
```mermaid
flowchart LR
    subgraph EntryPoints["Entry Point URLs"]
        E1["t.me/bot_name"]
        E2["t.me/bot_name?start=ref_ABC123"]
        E3["t.me/bot_name?start=campaign_123"]
        E4["t.me/bot_name?start=utm_source_facebook"]
    end
```

### 2️⃣ Onboarding Phase

```mermaid
flowchart TD
    subgraph OnboardingFunnel["Onboarding Funnel"]
        Start["/start<br/>100% of visitors"]
        Channel["Channel Check<br/>95% proceed"]
        Privacy["Privacy Policy<br/>90% accept"]
        Rules["Service Rules<br/>88% accept"]
        Language["Language Selection<br/>87% complete"]
        Menu["Main Menu<br/>85% reach"]
    end
    
    Start --> Channel --> Privacy --> Rules --> Language --> Menu
```

**Key Metrics:**
- Drop-off rate at each step
- Time to complete onboarding
- Language distribution
- Channel subscription rate

### 3️⃣ Trial Phase

```mermaid
gantt
    title Trial User Timeline
    dateFormat YYYY-MM-DD
    axisFormat %d
    
    section Trial Period
    Day 1 - Activation           :active, t1, 2024-01-01, 1d
    Day 2 - Active Usage        :active, t2, 2024-01-02, 1d
    Day 3 - Final Day           :crit, t3, 2024-01-03, 1d
    
    section Notifications
    Welcome Message             :milestone, m1, 2024-01-01, 0d
    Usage Tips (Day 2)          :milestone, m2, 2024-01-02, 0d
    Expiry Warning (Day 3)      :milestone, m3, 2024-01-03, 0d
    
    section Conversion Window
    Grace Period                :done, g1, 2024-01-04, 2d
```

**Trial Success Indicators:**
| Indicator | Good | Warning | At Risk |
|-----------|------|---------|---------|
| First Connection | < 1 hour | < 24 hours | Never |
| Traffic Used | > 50% | 20-50% | < 20% |
| Sessions | Daily | 2-3 total | 1 or less |
| App Downloads | Yes | - | No |

### 4️⃣ Conversion Phase

```mermaid
flowchart LR
    subgraph ConversionFunnel["Conversion Funnel"]
        TrialEnd["Trial Ended<br/>100 users"]
        Warning["Received Warning<br/>95 users"]
        Visit["Visited Bot<br/>70 users"]
        ViewPricing["Viewed Pricing<br/>50 users"]
        SelectPlan["Selected Plan<br/>35 users"]
        Payment["Completed Payment<br/>25 users"]
    end
    
    TrialEnd --> Warning --> Visit --> ViewPricing --> SelectPlan --> Payment
    
    ConversionRate["Conversion Rate: 25%"]
```

**Conversion Tactics:**
```mermaid
flowchart TD
    Tactic1["⏰ Time-Limited Discount"]
    Tactic2["📧 Reminder Messages"]
    Tactic3["💬 Show What They Lose"]
    Tactic4["💳 Multiple Payment Options"]
    Tactic5["🎁 Welcome Promo Code"]
    
    Tactic1 --> HigherConversion[Higher Conversion]
    Tactic2 --> HigherConversion
    Tactic3 --> HigherConversion
    Tactic4 --> HigherConversion
    Tactic5 --> HigherConversion
```

### 5️⃣ Active User Phase

**Daily Usage Pattern:**
```mermaid
flowchart LR
    subgraph DailyUsage["Typical Daily Usage"]
        Morning["🌅 Morning<br/>Connect VPN"]
        Day["☀️ Day<br/>Use Services"]
        Evening["🌆 Evening<br/>Check Stats"]
        Night["🌙 Night<br/>Disconnect"]
    end
    
    Morning --> Day --> Evening --> Night
```

**Engagement Touchpoints:**
```mermaid
flowchart TD
    subgraph Touchpoints["User Engagement Touchpoints"]
        TP1["📊 Weekly Usage Summary"]
        TP2["🚀 New Server Announcements"]
        TP3["🎁 Special Offers"]
        TP4["⚠️ Traffic Warnings"]
        TP5["💡 Feature Tips"]
        TP6["📣 Referral Reminders"]
    end
```

### 6️⃣ Renewal Phase

```mermaid
sequenceDiagram
    participant U as User
    participant B as Bot
    participant S as Scheduler
    
    Note over S: 7 days before expiry
    S->>B: Check expiring subs
    B->>U: First warning message
    
    Note over S: 3 days before expiry
    S->>B: Check expiring subs
    B->>U: Second warning + renewal link
    
    Note over S: 1 day before expiry
    S->>B: Check expiring subs
    B->>U: Urgent warning
    
    alt Auto-Pay Enabled
        S->>B: Process auto-renewal
        B->>U: Renewal confirmation
    else Manual Renewal
        U->>B: Click renew
        U->>B: Complete payment
        B->>U: Renewal confirmation
    end
```

### 7️⃣ Referral Phase

```mermaid
flowchart TD
    subgraph ReferralJourney["Referral User Journey"]
        R1["👤 Access Referral Menu"]
        R2["🔗 Copy Referral Link"]
        R3["📤 Share with Friends"]
        R4["👥 Friend Clicks Link"]
        R5["✅ Friend Registers"]
        R6["💳 Friend Tops Up"]
        R7["🎁 Both Get Bonuses"]
        R8["💰 Ongoing Commission"]
        R9["🏆 Contest Participation"]
        R10["💸 Withdrawal"]
    end
    
    R1 --> R2 --> R3 --> R4 --> R5 --> R6 --> R7 --> R8
    R8 --> R9
    R8 --> R10
```
- Regular value reminders
- Usage statistics
- Loyalty discounts
- Early renewal incentives

### 6️⃣ Renewal Phase

**Renewal Timeline:**

```mermaid
gantt
    title Subscription Renewal Timeline
    dateFormat  YYYY-MM-DD
    section Notifications
    Warning 1 (7 days)    :w1, 2024-01-24, 1d
    Warning 2 (3 days)    :w2, 2024-01-28, 1d
    Warning 3 (1 day)     :w3, 2024-01-30, 1d
    section Subscription
    Active Period         :active, 2024-01-01, 30d
    Renewal Window        :crit, 2024-01-28, 3d
    Expiry                :milestone, 2024-01-31, 0d
```

**Renewal Options:**

| Option | Trigger | User Effort |
|--------|---------|-------------|
| Auto-Pay | Enabled + Balance | Zero |
| Manual | Reminder → Action | Low |
| Lapsed | Post-expiry prompt | Medium |

### 7️⃣ Referral Phase

**Referral Funnel:**

```mermaid
flowchart TD
    Active[Active User] --> Share[Share Link]
    Share --> Click[Friend Clicks]
    Click --> Register[Friend Registers]
    Register --> Trial[Friend Trials]
    Trial --> TopUpBalance[Friend Tops Up]
    TopUpBalance --> Commission[Earn Commission]
```

**Referral Incentives:**

| Event | Referrer Gets | Referee Gets |
|-------|---------------|--------------|
| Registration | - | - |
| First Top-up | `REFERRAL_INVITER_BONUS` | `REFERRAL_FIRST_TOPUP_BONUS` |
| Each Payment | `REFERRAL_COMMISSION_PERCENT`% | - |

## User Lifecycle States

```mermaid
stateDiagram-v2
    [*] --> Visitor: Finds bot
    Visitor --> Registered: Completes onboarding
    Registered --> TrialUser: Activates trial
    TrialUser --> Converted: Purchases subscription
    TrialUser --> Churned: Trial expires, no purchase
    Converted --> ActivePaid: Using service
    ActivePaid --> Renewed: Auto/manual renewal
    ActivePaid --> Expired: Subscription ends
    Expired --> Reactivated: Purchases again
    Expired --> Churned: No return
    Churned --> Reactivated: Returns later
    Renewed --> ActivePaid
    Reactivated --> ActivePaid
```

## Touchpoints & Notifications

| Phase | Touchpoint | Message Type |
|-------|------------|--------------|
| Onboarding | Welcome | Greeting + instructions |
| Trial Start | Config delivery | Technical setup |
| Trial End | Warning | Urgency + value |
| Purchase | Confirmation | Receipt + next steps |
| Active | Usage updates | Engagement |
| Pre-expiry | Reminder | Renewal call |
| Post-expiry | Win-back | Discount offer |

## Metrics by Phase

| Phase | Key Metric | Target |
|-------|-----------|--------|
| Discovery | Reach | Growth |
| Onboarding | Completion rate | >90% |
| Trial | Activation rate | >70% |
| Conversion | Trial→Paid | >20% |
| Active | DAU/MAU | >30% |
| Renewal | Renewal rate | >80% |
| Referral | Referral rate | >10% |

## Optimization Opportunities

### Reduce Friction
- One-tap trial activation
- Seamless payment flow
- Auto-configuration for popular apps

### Increase Value
- Premium server access
- Higher speeds
- More device slots

### Build Loyalty
- Tenure-based discounts
- Referral rewards
- Exclusive features

---

**Related Diagrams:**
- [User Registration](./02-user-registration.md)
- [Trial Activation](./05-trial-activation.md)
- [Subscription Purchase](./03-subscription-purchase-classic.md)
- [Referral System](./08-referral-system.md)

