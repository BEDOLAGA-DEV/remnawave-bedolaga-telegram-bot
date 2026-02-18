# 🎫 Support Ticket System Flow

> Customer support workflow with ticket creation, SLA tracking, admin responses, and resolution.

## Overview

The support ticket system provides a structured way for users to request help and for administrators to manage support requests. It includes SLA (Service Level Agreement) tracking to ensure timely responses.

## Main Flow Diagram

```mermaid
flowchart TD
    User([User]) --> ClickSupport[Click Support Menu]
    ClickSupport --> CheckMode{Support Mode?}
    
    CheckMode -->|tickets| ShowTicketOption[Show Create Ticket]
    CheckMode -->|contact| ShowContact[Show Contact Info]
    CheckMode -->|both| ShowBothOptions[Show Both Options]
    
    ShowBothOptions --> UserChoice{User Choice}
    UserChoice -->|Ticket| ShowTicketOption
    UserChoice -->|Contact| ShowContact
    
    ShowContact --> DisplayUsername[Display support username]
    DisplayUsername --> EndContact([End])
    
    ShowTicketOption --> CheckBlocked{User Blocked?}
    CheckBlocked -->|Yes| ShowBlockedMessage[Show: You are blocked]
    ShowBlockedMessage --> EndBlocked([End])
    
    CheckBlocked -->|No| CheckActiveTicket{Has Active Ticket?}
    CheckActiveTicket -->|Yes| ShowExistingTicket[Show Existing Ticket]
    CheckActiveTicket -->|No| StartTicketCreation
    
    ShowExistingTicket --> TicketActions{Actions}
    TicketActions -->|Reply| AddReply[Add Reply]
    TicketActions -->|Close| UserCloseTicket[Close Ticket]
    TicketActions -->|View| ViewConversation[View Conversation]
    
    StartTicketCreation[Start Ticket Creation] --> EnterTitle[Enter Ticket Title]
    EnterTitle --> ValidateTitle{Title Valid?}
    ValidateTitle -->|Too Short| TitleError[Show: Title too short]
    TitleError --> EnterTitle
    ValidateTitle -->|Valid| EnterMessage[Enter Message]
    
    EnterMessage --> ValidateMessage{Message Valid?}
    ValidateMessage -->|Too Short| MessageError[Show: Message too short]
    MessageError --> EnterMessage
    ValidateMessage -->|Valid| CreateTicket[Create Ticket Record]
    
    CreateTicket --> SetStatus[Set Status: Open]
    SetStatus --> SetPriority[Set Priority: Normal]
    SetPriority --> StartSLA[Start SLA Timer]
    StartSLA --> SaveTicket[Save to Database]
    
    SaveTicket --> NotifyAdministrators[Notify Admin Group]
    NotifyAdministrators --> SendToTopic[Send to Tickets Topic]
    SendToTopic --> ShowConfirmation[Show: Ticket Created]
    ShowConfirmation --> TicketCreated([Ticket #ID Created])
    
    %% Admin Side
    AdminGroup([Admin Group]) --> ReceiveNotification[Receive Ticket Notification]
    ReceiveNotification --> AdminViews[Admin Views Ticket]
    AdminViews --> UpdateStatus[Update Status: In Progress]
    
    UpdateStatus --> AdminActions{Admin Actions}
    AdminActions -->|Reply| AdminReply[Write Reply]
    AdminActions -->|Close| AdminClose[Close Ticket]
    AdminActions -->|Block User| BlockUserFromTickets[Block User]
    AdminActions -->|Assign| AssignToAdmin[Assign to Admin]
    
    AdminReply --> SendReply[Send Reply]
    SendReply --> NotifyUser[Notify User]
    NotifyUser --> UpdateStatusAwait[Status: Awaiting User]
    UpdateStatusAwait --> WaitUserResponse
    
    WaitUserResponse([Wait for User]) --> UserResponds{User Responds?}
    UserResponds -->|Yes| AddReply
    AddReply --> NotifyAdministratorReply[Notify Admin: User Replied]
    NotifyAdministratorReply --> UpdateStatusInProgress[Status: In Progress]
    UpdateStatusInProgress --> AdminActions
    
    UserResponds -->|No Response| CheckTimeout{Response Timeout?}
    CheckTimeout -->|Yes| AutoClose[Auto-close Ticket]
    CheckTimeout -->|No| WaitUserResponse
    
    AdminClose --> SetResolved[Status: Resolved]
    UserCloseTicket --> SetResolved
    AutoClose --> SetResolved
    
    SetResolved --> RecordCloseTime[Record Close Time]
    RecordCloseTime --> NotifyTicketClosed[Notify: Ticket Closed]
    NotifyTicketClosed --> TicketResolved([Ticket Resolved])

    style User fill:#4CAF50
    style TicketCreated fill:#2196F3
    style TicketResolved fill:#2196F3
    style ShowBlockedMessage fill:#f44336
```

## SLA Monitoring Flow

```mermaid
flowchart TD
    Scheduler([Service Level Scheduler]) --> CheckInterval[Run Every SLA_CHECK_INTERVAL]
    CheckInterval --> QueryOpenTickets[Find open tickets]
    
    QueryOpenTickets --> ForEachTicket{For Each Ticket}
    
    ForEachTicket --> CalcWaitTime[Calculate Wait Time]
    CalcWaitTime --> CheckSLA{Wait Time > SLA Minutes?}
    
    CheckSLA -->|No| NextTicket[Next Ticket]
    CheckSLA -->|Yes| CheckReminderSent{Reminder Sent Recently?}
    
    CheckReminderSent -->|Yes, within cooldown| NextTicket
    CheckReminderSent -->|No| SendServiceLevelReminder[Send SLA Breach Alert]
    
    SendServiceLevelReminder --> FormatAlert
    subgraph FormatAlert["⚠️ SLA Alert"]
        A1["⚠️ SLA Breach!"]
        A2["Ticket #123"]
        A3["Title: {title}"]
        A4["User: @username"]
        A5["Waiting: 15 minutes"]
        A6["[View Ticket]"]
    end
    
    FormatAlert --> SendToAdmins[Send to Admin Group]
    SendToAdmins --> UpdateReminderTime[Update Last Reminder Time]
    UpdateReminderTime --> NextTicket
    
    NextTicket --> ForEachTicket
    ForEachTicket -->|All Processed| SchedulerDone([Check Complete])

    style Scheduler fill:#FF9800
```

## Ticket State Machine

```mermaid
stateDiagram-v2
    [*] --> Open: User Creates Ticket
    
    Open --> InProgress: Admin Views
    Open --> Resolved: User Closes
    
    InProgress --> AwaitingUser: Admin Replies
    InProgress --> Resolved: Admin Closes
    
    AwaitingUser --> InProgress: User Replies
    AwaitingUser --> Resolved: No Response (Timeout)
    AwaitingUser --> Resolved: Admin Closes
    
    Resolved --> [*]
    
    note right of Open: SLA Timer Active
    note right of InProgress: Admin Handling
    note right of AwaitingUser: Waiting for User
```

## Conversation View

```mermaid
flowchart TB
    subgraph TicketView["📋 Ticket #123: Connection Issues"]
        direction TB
        Header["Status: 🟡 In Progress | Priority: Normal"]
        
        subgraph Messages["Conversation"]
            M1["👤 User (10:00):<br/>I cannot connect to the VPN..."]
            M2["👨‍💼 Support (10:05):<br/>Please try restarting the app..."]
            M3["👤 User (10:10):<br/>Still not working after restart"]
            M4["👨‍💼 Support (10:15):<br/>Let me check your subscription..."]
        end
        
        Actions["[Reply] [Close Ticket]"]
    end
```

## Admin Ticket List View

```mermaid
flowchart LR
    subgraph TicketList["🎫 Active Tickets"]
        direction TB
        T1["🔴 #125 - Payment not received (45 min)"]
        T2["🟡 #124 - Speed issues (20 min)"]
        T3["🟢 #123 - Connection help (5 min)"]
        T4["🔵 #122 - Awaiting user response"]
    end
    
    Legend["🔴 SLA Breached | 🟡 Warning | 🟢 OK | 🔵 Awaiting"]
```

## Sequence Diagram

```mermaid
sequenceDiagram
    participant U as User
    participant B as Bot
    participant DB as PostgreSQL
    participant A as Admin Group
    participant S as SLA Scheduler
    
    U->>B: Click "Create Ticket"
    B->>DB: Check existing tickets
    DB-->>B: No active tickets
    B->>U: Enter title prompt
    U->>B: "VPN not connecting"
    B->>U: Enter message prompt
    U->>B: "I get error code 403..."
    
    B->>DB: Create ticket (status=open)
    B->>DB: Create first message
    B->>A: Send notification to Tickets topic
    B->>U: "Ticket #126 created"
    
    Note over S: SLA Scheduler runs
    S->>DB: Check open tickets
    
    A->>B: Admin clicks ticket notification
    B->>DB: Update status = in_progress
    B->>A: Show ticket details
    A->>B: Type reply
    B->>DB: Add message (is administrator=true)
    B->>DB: Update status = awaiting_user
    B->>U: "Support replied to ticket #126"
    
    U->>B: View ticket
    B->>DB: Load messages
    B->>U: Show conversation
    U->>B: Add reply
    B->>DB: Add message
    B->>DB: Update status = in_progress
    B->>A: "User replied to #126"
    
    A->>B: Close ticket
    B->>DB: Update status = resolved
    B->>DB: Set closed_at timestamp
    B->>U: "Ticket #126 has been resolved"
```

## User Blocking Flow

```mermaid
flowchart TD
    Admin([Admin]) --> ViewTicket[View User's Ticket]
    ViewTicket --> ClickBlockUser[Click Block User]
    
    ClickBlockUser --> SelectDuration{Block Duration}
    SelectDuration -->|Temporary| SetEndDate[Set End Date]
    SelectDuration -->|Permanent| SetPermanent[Set Permanent Block]
    
    SetEndDate --> EnterReason[Enter Block Reason]
    SetPermanent --> EnterReason
    
    EnterReason --> ConfirmBlock[Confirm Block]
    ConfirmBlock --> SaveBlock[Save to Database]
    SaveBlock --> CloseActiveTickets[Close Active Tickets]
    CloseActiveTickets --> NotifyUser[Notify User: Blocked]
    NotifyUser --> LogAction[Record admin activity]
```
