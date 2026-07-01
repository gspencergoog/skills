# Detailed proposal template

Use this template for major features, new products, or large-scale system
migrations.

## Title and metadata

- **Title**:
- **Author**:
- **Date**:
- **Status**: [Draft / Under Review / Approved]
- **Target audience**:

## Executive summary

Provide a summary of the problem, the proposed solution, and the expected
impact.

## Problem statement and context

Describe the background, the pain points, and the business or technical context.
Explain the impact of not solving this problem.

## Goals and non-goals

List the explicit goals and what is out of scope.

### Goals

- Goal 1
- Goal 2

### Non-goals

- Non-goal 1 (explicitly out of scope)
- Non-goal 2 (explicitly out of scope)

## Proposed architecture

Describe the architecture.

- Include a Mermaid diagram showing the components and data flow.
- Detail the communication protocols between components.

## Detailed design

Detail the implementation.

### Data storage and models

Describe the storage strategy (e.g., database choice, caching) and schemas.

### API design

Detail the API contracts, protocols (e.g., gRPC, REST), and payloads.

### Key algorithms

Describe any complex logic or algorithms.

## Cross-cutting concerns

### Security and privacy

Explain how data is protected, how users are authenticated, and how
authorization is handled.

### Performance and scalability

Describe the expected load, resource requirements, and how the system scales.

### Observability

Detail the logging, metrics, and alerting strategies.

### Reliability

Explain the failover mechanisms, data backup strategies, and recovery
procedures.

## Implementation plan

### Milestones

Break down the work into phases or milestones.

### Testing strategy

Explain how the changes will be verified (e.g., unit tests, integration tests,
load tests).

## Alternatives considered

List other solutions you evaluated and why they were rejected.

## Risks and mitigations

Identify potential risks and how you plan to address them.
