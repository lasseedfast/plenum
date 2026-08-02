# **Scorer Integration Guide (RTX 3060 / vLLM)**

This guide adds a coverage\_score column to your Swedish Parliament evaluation harness using a high-speed "Cross-Encoder" (Reranker) model.

## **1\. How it works**

* **The Model:** BAAI/bge-reranker-v2-m3 acts as a "Scorer." It looks at the Source and the Paragraph simultaneously and outputs a relevance score.  
* **The API:** We use vLLM's /v1/score (or /v1/rerank) endpoint. It is 10x faster than Qwen because it doesn't generate text; it only computes a single mathematical "head".  
* **The GPU:** On your RTX 3060, this runs in the background with very low VRAM usage (\~2GB if quantized).

## **2\. Infrastructure Setup**

Run this command on your Debian server to start the scorer. vLLM will automatically download the model from HuggingFace on the first run.  
docker run \--gpus all \\  
  \-p 8001:8000 \\  
  \--name eval-scorer \\  
  vllm/vllm-openai \\  
  \--model BAAI/bge-reranker-v2-m3 \\  
  \--device cuda \\  
  \--max-model-len 4096 \\  
  \--gpu-memory-utilization 0.2 \\  
  \--trust-remote-code

## **3\. Database & Code Integration**

### **Step A: Update SQL**

ALTER TABLE eval\_judgments ADD COLUMN coverage\_score FLOAT DEFAULT 0.0;

### **Step B: The Python Logic (Add to eval\_harness.py)**

Add this class to handle the communication with vLLM. Note the use of the sigmoid function to turn the model's "logits" into a 0-1 probability.  
import requests  
import math

class CitationScorer:  
    """Connects to the vLLM /v1/score endpoint."""  
    def \_\_init\_\_(self, endpoint: str \= "http://localhost:8001/v1/score"):  
        self.endpoint \= endpoint

    def get\_score(self, paragraph: str, sources: str) \-\> float:  
        """Calculates a support probability (0.0 to 1.0)."""  
        try:  
            payload \= {  
                "model": "BAAI/bge-reranker-v2-m3",  
                "text\_1": sources\[:12000\], \# Truncate long sources for speed  
                "text\_2": paragraph  
            }  
            response \= requests.post(self.endpoint, json=payload, timeout=5)  
            if response.status\_code \== 200:  
                \# BGE-Reranker-v2 outputs logits. Sigmoid converts to 0-1.  
                data \= response.json().get("data", \[\])  
                if data:  
                    raw\_logit \= data\[0\].get("score", \-10.0)  
                    return 1 / (1 \+ math.exp(-raw\_logit))  
            return 0.0  
        except Exception as e:  
            print(f"\[scorer\] Error calling vLLM: {e}")  
            return 0.0

### **Step C: Update the Main Evaluation Loop**

In eval\_harness.py, modify the section where you process judgments:  
\# Initialize once at start  
scorer \= CitationScorer()

\# ... inside the paragraph loop ...  
try:  
    \# 1\. Get the standard LLM verdict (Qwen/etc)  
    judgments \= judge.verdict(answer, sources\_compact)  
      
    \# 2\. Add the quantitative Scorer verdict  
    for j in judgments:  
        p\_text \= j.get("paragraph\_text", "")  
        \# The scorer gives a 0-1 confidence that the source supports this paragraph  
        j\["coverage\_score"\] \= scorer.get\_score(p\_text, sources\_compact)

    \# 3\. Save to Postgres (Ensure your insert\_judgments helper handles this key)  
    insert\_judgments(question\_id, judgments, judge\_model)  
except Exception as e:  
    print(f"Error in judge/scorer loop: {e}")

## **4\. Verification**

To verify it's working without running the whole script:  
curl http://localhost:8001/v1/score \\  
  \-H "Content-Type: application/json" \\  
  \-d '{  
    "model": "BAAI/bge-reranker-v2-m3",  
    "text\_1": "The Riksdag consists of 349 members.",  
    "text\_2": "There are 349 politicians in the Swedish parliament."  
  }'

*(You should see a high positive score).*