# MicroTx Installation Guide Reference

This file indexes the bundled Oracle MicroTx Installation and Configuration Guide PDF:

- `oracle-microtx-installation-and-configuration-guide.pdf`
- Title: MicroTx Installation and Configuration Guide
- Release focus visible in the guide outline: MicroTx 26.1
- PDF metadata date: 2026-05-24

Use the original PDF when the user asks for exact installation commands, supported-version details, required YAML keys, Kubernetes/Istio/identity-provider setup, Docker Compose setup, upgrade procedure, or environment-variable names. This companion file is a routing aid, not a substitute for the full guide text.

## When to consult this guide

Read or inspect the PDF for tasks involving:

- Planning a new MicroTx installation.
- Installing MicroTx on Kubernetes.
- Installing or running MicroTx in Docker Compose or a local Docker container.
- Setting up Oracle Identity Provider, IAM, RBAC, access tokens, refresh tokens, or transaction-token security.
- Preparing Kubernetes, Istio, ingress, registry secrets, TLS details, encryption keys, or transaction-token key pairs.
- Configuring MicroTx Web Console access.
- Configuring Oracle Database or etcd as a datastore.
- Editing Helm `values.yaml` for coordinator, workflow server, console, security, data store, cache, retry, encryption, transaction token, event handler, task, or file-storage settings.
- Upgrading MicroTx, including Kubernetes, Docker, and SQL-script upgrade paths.
- Configuring the coordinator with environment variables.

## Guide Outline

The PDF outline includes these installation and configuration sections:

- 1 About Transaction Manager for Microservices
- 1.1 High-Level Tasks to Install MicroTx
- 1.2 Quick Start
- 2 Plan
- 2.1 Supported Container Platforms
- 2.2 Supported Authorized Cloud Environments
- 2.3 Supported Languages and Frameworks
- 2.4 Supported Data Stores
- 2.5 Supported Identity Providers
- 2.6 About Authentication and Authorization
- 2.6.1 About Access and Refresh Tokens
- 2.6.2 About Encrypting and Storing Tokens
- 2.6.3 About the Oracle_Tmm_Tx_Token Transaction Token
- 2.6.4 Overview of Role-Based Access Control for MicroTx Workflows
- 3 Prepare
- 3.1 Download the Installation Bundle
- 3.2 Download the MicroTx images from Oracle Container Registry
- 3.3 Set Up Oracle Identity Providers
- 3.3.1 Use Oracle IAM as Identity Provider
- 3.3.2 Run the Discovery URL
- 3.3.3 Create an Access Token
- 3.4 Prepare a Kubernetes Cluster
- 3.4.1 Considerations for Deployment on Kubernetes
- 3.4.2 Create a Kubernetes Cluster
- 3.4.3 Install the Required Software for Kubernetes
- 3.4.4 Install and Configure Istio
- 3.4.5 Create a Kubernetes Secret with SSL Details for Istio
- 3.4.6 Create a Kubernetes Secret to Access Docker Registry
- 3.4.7 Authenticate and Authorize
- 3.4.7.1 Generate a Kubernetes Secret for an Encryption Key
- 3.4.7.2 Create a Key Pair for Transaction Token
- 3.5 Set Up Access to MicroTx Web Console
- 3.5.1 Specify the Admin Role in YAML file
- 3.5.2 Create a Secret with Identity Provider Client Credentials
- 3.5.3 Create a Secret with Cookie Encryption Password for Kubernetes
- 3.5.4 Deploy Kubernetes Metrics Server
- 3.6 Set Up Oracle Database as Data Store
- 3.6.1 Prerequisites
- 3.6.2 Grant Privileges to Database User
- 3.6.3 Get Autonomous Database Client Credentials
- 3.6.4 Create Tables in Oracle Database
- 3.6.5 Create a Kubernetes Secret for Oracle Database Credentials
- 3.7 Set Up etcd as Data Store for MicroTx Distributed Transactions
- 3.7.1 Generate RSA Certificates for etcd
- 3.7.2 Create a Kubernetes Secret for etcd
- 4 Install on a Kubernetes Cluster
- 4.1 Push Images to a Remote Docker Repository
- 4.2 Configure the values.yaml File
- 4.2.1 Namespace Configuration
- 4.2.2 Istio Details
- 4.2.3 Common Configuration
- 4.2.4 MicroTx Distributed Transactions Coordinator Configuration
- 4.2.4.1 Image Properties
- 4.2.4.2 MicroTx Distributed Transactions Coordinator Properties
- 4.2.4.3 Retry Setting Properties
- 4.2.4.4 Caching Properties
- 4.2.4.5 Data Store Properties
- 4.2.4.6 Encryption Key Properties
- 4.2.4.7 Transaction Token Properties
- 4.2.5 Security Configuration Properties
- 4.2.5.1 Identity Provider Properties
- 4.2.5.2 Role Mapping for MicroTx Distributed Transactions Coordinator
- 4.2.5.3 Role Mapping for MicroTx Workflows
- 4.2.5.4 Authorization Properties
- 4.2.5.5 Authentication Properties
- 4.2.6 Console Configuration Properties
- 4.2.7 MicroTx Workflows Server Configuration
- 4.2.7.1 Data Store Properties
- 4.2.7.2 Encryption Properties
- 4.2.7.3 Task Configuration Properties
- 4.2.7.4 Event Handler Configuration
- 4.2.7.5 File System Storage Properties
- 4.3 Install MicroTx
- 4.4 Access MicroTx
- 4.5 Check the Server Health
- 4.6 Find IP Address of Istio Ingress Gateway
- 5 Upgrade to MicroTx 26.1
- 5.1 Upgrade to the Latest Free Release
- 5.2 Back Up Cached Maintenance Data
- 5.3 Upgrade to the Latest Enterprise Edition in Kubernetes Cluster
- 5.4 Upgrade to the Latest Enterprise Edition in Docker
- 5.5 Upgrade to the Latest Enterprise Edition Using SQL Scripts
- A Install on Docker Compose
- B Run MicroTx Distributed Transactions in a Docker Container in Local Environment
- B.1 Run MicroTx Distributed Transactions in a Docker Container on Linux
- B.2 Run MicroTx Distributed Transactions in a Docker Container on Windows
- B.3 Run MicroTx Distributed Transactions in a Docker Container on macOS (Intel x86)
- B.4 Run MicroTx Distributed Transactions in a Docker Container on macOS (Arm)
- C Configure Coordinator Using Environment Variables
- C.1 Provide Configuration Details
- C.2 Environment Variables for Transaction Coordinator

## Installation Guidance Rules

- Ask for the target environment first: Kubernetes, Docker Compose, local Docker container, or upgrade.
- Ask whether the user is installing Free or Enterprise Edition when the answer affects image access, upgrade paths, or supported features.
- Confirm where secrets should live before generating commands: Kubernetes secret, local environment variable, shell profile, or CI/CD secret store.
- Redact all credentials, tokens, private keys, wallet data, and registry passwords in any displayed command or YAML unless the user explicitly asks to see a local-only example with placeholders.
- For Kubernetes work, identify namespace, image registry/repository, Istio ingress mode, datastore choice, identity provider, and TLS/certificate strategy before writing final commands.
- For workflow-server installation questions, cross-reference `workflows-guide.md`, `task-types.md`, `connectors-guide.md`, and `agentic-ai-guide.md` after installation basics are clear.
