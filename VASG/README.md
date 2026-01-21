## Description
### Motivation
Many existing public intrusion datasets e.g. DARPA / ADFA are outdated and no longer representative of current environments. In order to expand public intrusion data, we aim to design a work flow that generates synthetic audit logs. 
- **Goal**: existing audit logs $\to$ synthetic audit logs.

### Design

A naive approach would be to simply train a LLM to output synthetic audit logs, given existing audit logs as input. However, human cannot verify whether the model has really captured the essence of the attacks. Thus, we design the workflow to extract human-readable attack features from inputs (audit logs) and use it to verify what the model has learned.
![VASG structure](https://hackmd.io/_uploads/SksnjsrVxx.png)

1. **Log2Feat**: *existing audit logs $\to$ human-readable features.*
    We train this model to extract human-readable features from given audit logs. Our approach is to use DPO with RLHF (human-labeled preference) to train this model. 
2. **Feat2Log**: *human-readable features $\to$ synthetic audit logs.*
    We train this model to generate synthetic logs, given the features extracted from Log2Feat stage. Our approach is to use instruction fine tuning on CodeLlama to generate new logs. 
    
To complete this workflow, we need to:
1. Collect a human preference dataset.
2. Use the human preference dataset to train log2feat to generate features.
3. Perform infilling training on log2feat so it can learn the structure of logs.
4. Fine-tune log2feat with instructions to teach it how to transform features into log files.

### Current Progress
#### Log2Feat: audit log recording

Before training Log2Feat model with existing audit logs, we have to make sure that the logs **successfully** recorded the attack path. To verify this, we pre-processed the logs and then prompted ChatGPT to extract necessary information from the processed logs. We find that default SPADE settings would've missed some important indication of attacks e.g. it only records systems calls when the system call successfully executed and returned. Thus, we modified some SPADE code, and verified such modified code sould successfully trace the attack paths. 

Here we also define essential information required in feature extraction. 
1. Process execution timeline: 
    ![image](https://hackmd.io/_uploads/Hy-4GLKNlx.png)
2. Process hierarchy:
    ![image](https://hackmd.io/_uploads/HkJwzIFVlg.png)

3. Process-File interactions:
    ![image](https://hackmd.io/_uploads/HJMiMUYVgl.png)

4. Process-Action mappings:
    ![image](https://hackmd.io/_uploads/Sku2zLFVlg.png)

5. Behavior summary & Threat insights:
    ![image](https://hackmd.io/_uploads/Skk0MLFVxg.png)

#### Feat2Log: Infilling Training

To complete the Feat2Log model, two steps are required: infilling training and instruction fine-tuning. We have finished the infilling training stage, where each log line is divided into three parts—prefix, middle, and suffix—and the model is trained to predict the missing middle part. This approach helps the model learn the underlying structure of audit logs. 

![alt text](imgs_for_README/pre_mid_suf.png)

We use 160,000 log lines from benign logs (see feat2log/data for details), splitting them into 100,000 for training, 10,000 for validation, and 50,000 for testing. The training hyperparameters can be found in feat2log/config.yaml.

Our results show that infilling training leads to a 114.46% improvement on the infilling task and a 72.6% improvement on the generation task, demonstrating that infilling training also benefits log generation performance.


## About this Repo
This repository contains two main pipelines:

1. **Log2Feat Pipeline** (`log2feat/`): Convert raw audit logs into human-readable features using ChatGPT
- **Purpose**: Extract human-readable features from audit logs
- **Method**: Uses ChatGPT to analyze processed log data
- **Output**: Structured feature descriptions for security analysis
2. **Feat2Log Pipeline** (`feat2log/`): Train and evaluate CodeLlama models for log infilling and generation tasks
- **Purpose**: Train models to understand and generate audit log patterns
- **Method**: LoRA fine-tuning of CodeLlama models using infilling tasks
- **Evaluation**: Both infilling (fill-in-the-blank) and generation 

- **Modes**: 
  - `--example`: Quick testing with 10 samples
  - Full training: Complete dataset training

For detailed instructions on each pipeline, please refer to the README files in the respective directories.
