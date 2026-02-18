# 💳 Payment Processing Flow

> Comprehensive payment processing through 11 different payment providers.

## Overview

The bot supports multiple payment methods to maximize conversion. All payments credit the user's internal balance, which can then be used for subscription purchases.

## Main Flow Diagram

```mermaid
flowchart TD
    TopUpBalance([Top-up Balance]) --> CheckMethods[Load Enabled Payment Methods]
    CheckMethods --> ShowMethods[Display Payment Options]
    
    ShowMethods --> SelectMethod{User Selects Method}
    
    SelectMethod -->|⭐ Telegram Stars| StarsFlow
    SelectMethod -->|₿ CryptoBot| CryptoBotFlow
    SelectMethod -->|🪙 Heleket| HeleketFlow
    SelectMethod -->|🏦 YooKassa| YooKassaFlow
    SelectMethod -->|💵 MulenPay| MulenPayFlow
    SelectMethod -->|📱 PAL24| PAL24Flow
    SelectMethod -->|💳 Platega| PlategaFlow
    SelectMethod -->|🌊 WATA| WATAFlow
    SelectMethod -->|🔗 Freekassa| FreekassaFlow
    SelectMethod -->|☁️ CloudPayments| CloudPayFlow
    SelectMethod -->|🎁 Tribute| TributeFlow
    
    subgraph StarsFlow["⭐ Telegram Stars"]
        S1[Enter Amount in Stars] --> S2[Create payment request]
        S2 --> S3[Show Payment Button]
        S3 --> S4[User Confirms in TG Dialog]
        S4 --> S5[pre_checkout_query Event]
        S5 --> S6[Validate and Approve]
        S6 --> S7[successful_payment Event]
    end
    
    subgraph CryptoBotFlow["₿ CryptoBot"]
        CB1[Enter Amount] --> CB2[Select Crypto Asset]
        CB2 --> CB3[Create Invoice via API]
        CB3 --> CB4[Show Payment Link]
        CB4 --> CB5[User Pays in CryptoBot]
        CB5 --> CB6[Webhook: invoice.paid]
    end
    
    subgraph HeleketFlow["🪙 Heleket"]
        H1[Enter Amount] --> H2[Select Currency]
        H2 --> H3[Create payment request]
        H3 --> H4[Show Payment Page Link]
        H4 --> H5[User Pays]
        H5 --> H6[Payment confirmed]
    end
    
    subgraph YooKassaFlow["🏦 YooKassa"]
        Y1[Enter Amount] --> Y2{Payment Type}
        Y2 -->|Card| Y3[Create Card Payment]
        Y2 -->|SBP| Y4[Create SBP Payment]
        Y3 --> Y5[Redirect to Bank Page]
        Y4 --> Y5
        Y5 --> Y6[User Completes Payment]
        Y6 --> Y7[Webhook: payment.succeeded]
    end
    
    subgraph PAL24Flow["📱 PAL24"]
        P1[Enter Amount] --> P2{Method}
        P2 -->|SBP| P3[Create SBP Payment]
        P2 -->|Card| P4[Create Card Payment]
        P3 --> P5[Show QR Code]
        P4 --> P6[Show Payment Link]
        P5 --> P7[Payment confirmed]
        P6 --> P7
    end
    
    S7 --> ProcessPayment
    CB6 --> ProcessPayment
    H6 --> ProcessPayment
    Y7 --> ProcessPayment
    P7 --> ProcessPayment
    
    MulenPayFlow[MulenPay Flow] --> ProcessPayment
    PlategaFlow[Platega Flow] --> ProcessPayment
    WATAFlow[WATA Flow] --> ProcessPayment
    FreekassaFlow[Freekassa Flow] --> ProcessPayment
    CloudPayFlow[CloudPayments Flow] --> ProcessPayment
    TributeFlow[Tribute Flow] --> ProcessPayment
    
    ProcessPayment[Process payment] --> ValidatePayment{Validate Payment}
    
    ValidatePayment -->|Invalid Signature| RejectPayment[Reject]
    ValidatePayment -->|Duplicate| SkipDuplicatePayment[Skip Processing]
    ValidatePayment -->|Valid| FindUser[Find user]
    
    RejectPayment --> LogRejection[Record security issue]
    LogRejection --> EndReject([End])
    
    FindUser --> UserFound{User Found?}
    UserFound -->|No| LogOrphanPayment[Log Orphan Payment]
    LogOrphanPayment --> NotifyAdministratorOrphan[Alert Admin]
    NotifyAdministratorOrphan --> EndOrphan([End])
    
    UserFound -->|Yes| ConvertCurrency{Convert to RUB?}
    ConvertCurrency -->|Crypto| ApplyRate[Apply Exchange Rate]
    ConvertCurrency -->|Stars| ApplyStarsRate[Apply Stars Rate]
    ConvertCurrency -->|RUB| NoConversion[No Conversion]
    
    ApplyRate --> CalculateAmount
    ApplyStarsRate --> CalculateAmount
    NoConversion --> CalculateAmount
    
    CalculateAmount[Calculate Amount in Kopeks] --> BeginTransactionaction[Process purchase]
    
    BeginTransactionaction --> AddBalance[Add to balance]
    AddBalance --> CreateTransactionRecord[Create transaction record]
    CreateTransactionRecord --> CheckReferralerral{Has Referrer?}
    
    CheckReferralerral -->|Yes| CalculateCommission[Calculate Referral Commission]
    CalculateCommission --> AddReferralBonus[Add to Referrer Balance]
    AddReferralBonus --> CreateReferralTrans[Record referral bonus]
    CreateReferralTrans --> CheckFirstTopup
    
    CheckReferralerral -->|No| CheckFirstTopup{First Top-up?}
    
    CheckFirstTopup -->|Yes with Referrer| ApplyBonuses[Apply First Top-up Bonuses]
    ApplyBonuses --> CommitTransactionaction
    CheckFirstTopup -->|No| CommitTransactionaction
    
    CommitTransactionaction[Complete operation] --> CheckNalogo{NaloGO Enabled?}
    
    CheckNalogo -->|Yes| QueueReceipt[Queue Tax Receipt]
    QueueReceipt --> NotifyUser
    CheckNalogo -->|No| NotifyUser
    
    NotifyUser[Notify User] --> SendConfirmation[Send Payment Confirmation]
    SendConfirmation --> NotifyAdministrator[Notify Admin Group]
    
    NotifyAdministrator --> CheckCart{Cart Has Items?}
    CheckCart -->|Yes| CheckAutoPurchase{Auto-Purchase Enabled?}
    CheckAutoPurchase -->|Yes| TriggerAutoPurchase[Trigger Auto-Purchase]
    CheckAutoPurchase -->|No| PaymentComplete
    TriggerAutoPurchase --> ProcessCart[Process Cart Items]
    ProcessCart --> PaymentComplete
    CheckCart -->|No| PaymentComplete([Payment Complete])

    style TopUpBalance fill:#4CAF50
    style PaymentComplete fill:#2196F3
    style RejectPayment fill:#f44336
```

## Webhook Processing Detail

```mermaid
flowchart TD
    WebhookReceived([Payment notification received]) --> ExtractHeaders[Extract Headers]
    ExtractHeaders --> DetermineProvider{Determine Provider}
    
    DetermineProvider -->|/yookassa-webhook| YKValidate
    DetermineProvider -->|/cryptobot-webhook| CBValidate
    DetermineProvider -->|/heleket-webhook| HKValidate
    DetermineProvider -->|/tribute-webhook| TBValidate
    DetermineProvider -->|/mulenpay-webhook| MPValidate
    DetermineProvider -->|/pal24-webhook| P24Validate
    DetermineProvider -->|/platega-webhook| PLValidate
    DetermineProvider -->|/wata-webhook| WTValidate
    DetermineProvider -->|/freekassa-webhook| FKValidate
    DetermineProvider -->|/cloudpayments-webhook| CPValidate
    
    subgraph YKValidate["YooKassa Validation"]
        YK1[Verify sender is authorized] --> YK2{IP Allowed?}
        YK2 -->|No| YKReject[Reject as unauthorized]
        YK2 -->|Yes| YK3[Read payment details]
        YK3 --> YK4[Check notification type]
        YK4 --> YK5{payment.succeeded?}
        YK5 -->|Yes| YKProcess[Process]
        YK5 -->|No| YKAck[Acknowledge]
    end
    
    subgraph CBValidate["CryptoBot Validation"]
        CB1[Get Signature Header] --> CB2[Verify security code]
        CB2 --> CB3{Signatures Match?}
        CB3 -->|No| CBReject[Reject as unauthorized]
        CB3 -->|Yes| CB4[Read payment details]
        CB4 --> CB5[Get payment information]
        CB5 --> CBProcess[Process]
    end
    
    subgraph HKValidate["Heleket Validation"]
        HK1[Get Sign Header] --> HK2[Verify security code]
        HK2 --> HK3{Signatures Match?}
        HK3 -->|No| HKReject[Reject as unauthorized]
        HK3 -->|Yes| HK4[Parse Body]
        HK4 --> HKProcess[Process]
    end
    
    YKProcess --> CommonProcess
    CBProcess --> CommonProcess
    HKProcess --> CommonProcess
    
    CommonProcess[Common Processing] --> ExtractPaymentData[Extract Payment Data]
    ExtractPaymentData --> ExtractUserId[Extract User ID from Payload]
    ExtractUserId --> CheckIdempotency{Already Processed?}
    CheckIdempotency -->|Yes| Return200[Confirm receipt]
    CheckIdempotency -->|No| ProcessPayment[Process payment]
    ProcessPayment --> MarkProcessed[Mark as completed]
    MarkProcessed --> Return200
```

## Payment Provider Details

| Provider | Type | Webhook Port | Validation | Min Amount | Max Amount |
|----------|------|--------------|------------|------------|------------|
| Telegram Stars | Native | - | Built-in | - | - |
| CryptoBot | Crypto | 8083 | HMAC-SHA256 | - | - |
| Heleket | Crypto | 8086 | MD5 | - | - |
| YooKassa | Fiat | 8082 | IP + Object | 50₽ | 10,000₽ |
| MulenPay | Fiat | - | Signature | 100₽ | 100,000₽ |
| PAL24 | Fiat | - | Token | 100₽ | 1,000,000₽ |
| Platega | Fiat | 8086 | Secret Hash | 100₽ | 1,000,000₽ |
| WATA | Fiat | - | Token | - | - |
| Freekassa | Fiat | - | Secret Words | - | - |
| CloudPayments | Fiat | - | Signature | - | - |
| Tribute | Donation | 8081 | HMAC-SHA256 | - | - |

## Currency Conversion

```mermaid
flowchart LR
    subgraph Conversion["Currency Conversion"]
        Stars[Stars] -->|× TELEGRAM_STARS_RATE_RUB| RUB1[RUB]
        USDT[USDT] -->|× USD/RUB Rate| RUB2[RUB]
        BTC[BTC] -->|× BTC/RUB Rate| RUB3[RUB]
        ETH[ETH] -->|× ETH/RUB Rate| RUB4[RUB]
        TON[TON] -->|× TON/RUB Rate| RUB5[RUB]
    end
    
    RUB1 --> Kopeks[Convert to Kopeks]
    RUB2 --> Kopeks
    RUB3 --> Kopeks
    RUB4 --> Kopeks
    RUB5 --> Kopeks
```

## Tax Receipt Flow (NaloGO)

```mermaid
flowchart TD
    PaymentSuccess([Payment Success]) --> CheckNalogoEnabled{NaloGO Enabled?}
    CheckNalogoEnabled -->|No| Skip[Skip Receipt]
    CheckNalogoEnabled -->|Yes| QueueReceipt[Add to Receipt Queue]
    
    QueueReceipt --> BackgroundJob[Background Job Processes]
    BackgroundJob --> PrepareData[Prepare Receipt Data]
    PrepareData --> CallTaxServiceInterface[Call NaloGO API]
    
    CallTaxServiceInterface --> APIResult{Success?}
    APIResult -->|Yes| StoreReceipt[Store Receipt ID]
    StoreReceipt --> MarkComplete[Mark as Sent]
    
    APIResult -->|No| IncrementRetry[Increment Retry Count]
    IncrementRetry --> CheckMaxRetries{Max Retries?}
    CheckMaxRetries -->|No| RequeueDelay[Requeue with Delay]
    CheckMaxRetries -->|Yes| MarkFailed[Mark as Failed]
    MarkFailed --> NotifyAdministratorFailed[Notify Admin]
```
