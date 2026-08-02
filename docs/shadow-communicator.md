# **Technical Guide: Implementing the Shadow Communicator Pattern with vLLM**

This guide describes how to optimize a multi-agent "Research \+ Communication" loop using vLLM's **Automatic Prefix Caching (APC)**.  
By utilizing a parallel "Shadow Communicator," we can provide real-time research updates to the user with near-zero additional GPU cost.

## **1\. Deep Dive: How vLLM KV Caching Works**

To make the "Shadow Communicator" efficient, you must align your requests with vLLM’s internal caching mechanics.

### **Block-Based Hashing**

Unlike standard LRU caches that store full strings, vLLM divides the input prompt into **fixed-size blocks** (usually 16 tokens).

* Each block is hashed based on its content.  
* If a new request shares a prefix of blocks with a previous request, vLLM reuses the **Key-Value (KV) tensors** already stored in GPU memory.  
* This skips the "Prefill" phase (the expensive matrix multiplications required to "read" the prompt).

### **Positional Dependency (The "Anchor" Rule)**

In Transformer models, the KV pairs for token $N$ depend on every token from $1$ to $N-1$.

* **Requirement:** For a cache hit to occur, the token sequence must be **identical from the very first token**.  
* **The Loop Advantage:** Because our chat.py loop always appends new tool results to the *end* of the history, the prefix (system prompt \+ past history) stays stable.  
* **The Result:** The GPU only spends time computing the tokens for the brand-new tool result and the communicator's specific instructions. The entire history is "read" for free.

## **2\. Architectural Pattern: The Shadow Communicator**

Instead of the "Orchestrator" (Smart LLM) deciding when to talk, we implement an **Observer Pattern**.

### **Workflow in \_run\_tool\_loop:**

1. **Tool Execution:** Orchestrator calls a tool (e.g., arango\_search).  
2. **Result Processing:** The tool result is returned and formatted.  
3. **Parallel Observation:** Immediately after the result is formatted, fire a separate request to the **Fast LLM** (The Communicator).  
4. **Non-Blocking Feedback:** If the Communicator finds the result interesting, it triggers the event\_callback (UI update). The Orchestrator continues its research loop simultaneously.

## **3\. Implementation Steps for the AI Assistant**

### **Step 1: Tool result injection**

Ensure that when a tool returns data, the result is appended to the message history immediately. This "warms up" the cache for the Communicator.

### **Step 2: Cache-Aligned Prompting**

To ensure the Communicator hits the vLLM cache, its prompt structure must mirror the Orchestrator's:  
**Communicator Payload Structure:**

1. \[Orchestrator System Prompt\] (Identical start)  
2. \[Full Conversation History\] (Identical middle)  
3. \[Latest Tool Result\] (The trigger)  
4. \[Instruction Suffix\] (Unique end: "Is this worth sharing? If yes, write 1 sentence in Swedish. If no, say 'SKIP'.")

### **Step 3: Handling the "Ghost Content"**

Since the Communicator runs as a side-effect, its output should **never** be appended to the main conversation history (current\_messages). This keeps the Orchestrator’s context "clean" and focused only on raw data and research logic.

## **4\. Why this is superior to "share\_insight" as a tool**

1. **Reduces Cognitive Load:** The Orchestrator doesn't have to "think" about UX/politeness.  
2. **Reliability:** You don't have to hope the LLM calls the tool; every data-heavy result is automatically checked for "insight-worthiness."  
3. **Performance:** Because of vLLM's APC, running this check costs only a few dozen tokens of "generation" time, as the "prefill" of the history is cached.

## **5\. Implementation Guardrails**

* **No Timestamps:** Do not inject dynamic timestamps into the system prompt mid-loop, as this changes the start of the string and breaks the cache.  
* **Fast Model Preference:** Use your "Fast" model for the Communicator to keep the UI snappy.  
* **Strict Exit:** Ensure the Communicator has a strict "Negative" trigger (like the word 'SKIP') to avoid redundant small talk.