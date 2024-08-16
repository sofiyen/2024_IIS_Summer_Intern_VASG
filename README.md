# Various Attack Scenario Generation

### What is VASG? and Why?
Many existing public intrusion datasets, such as DARPA and ADFA, are outdated and no longer representative of current environments.

- ***Purpose*** : Generate new datasets for public intrusion $\Rightarrow$ diversify and update to current environments.

- ***Idea*** : 
  - *Step 1* : Record audit logs of various scenarios $\Rightarrow$ in this case, **attack scenarios** with CALDERA and **benign scenarios** with stress-ng.

  - *Step 2* : Develop synthetic dataset generation algorithms to merge multiple scenarios into one.

- ***Current Progress*** : Process automation complete for step 1 (CALDERA and stress-ng).

![Core Idea](image.png)

***Two parts for implementation :***
1. [Part 1 : Attack Scenario with CALDERA](#part-1--attack-scenario-with-caldera)
2. [Part 2 : Benign Scenario with stress-ng](#part-2--benign-scenario-with-stress-ng)

<details>
  <summary><b><i>Folder Structure</i></b></summary>
  <details style="margin-left: 20px;">
    <summary>1. </summary>
  </details>
  <details style="margin-left: 20px;">
    <summary>2. </summary>
  </details>
  <details style="margin-left: 20px;">
    <summary>3. </summary>
  </details>
</details>

---

### Part 1 : Attack Scenario with CALDERA 

<details>
  <summary><b><i>Environment Set-Up</i></b></summary>
  <details style="margin-left: 20px;">
    <summary>1. </summary>
  </details>
  <details style="margin-left: 20px;">
    <summary>2. </summary>
  </details>
  <details style="margin-left: 20px;">
    <summary>3. </summary>
  </details>
</details>

<details>
  <summary><b><i>Potential Problems</i></b></summary>
  <details style="margin-left: 20px;">
    <summary>1. </summary>
  </details>
  <details style="margin-left: 20px;">
    <summary>2. </summary>
  </details>
  <details style="margin-left: 20px;">
    <summary>3. </summary>
  </details>
</details>

---

### Part 2 : Benign Scenario with stress-ng
> 💡 In this part, we use stress-ng to simulate system operations.
> - **Benign_Collect_vm** : server with SPADE to record audit logs.
> - **Benign_Transform_vm** : server that transform audit logs into provenance graphs.

#### Environment
- **Benign_Collect_vm** : static IP `192.168.56.210`  

  - *Network Setup* : 3 adapters
    ```
    - Adapter 1 : NAT (for set-up)
    - Adapter 2 : Internal Network (communication with Benign_User)
    - Adapter 3 : Host-Only (communication with host OS)
    ``` 
    - Network configuration @ `/etc/netplan/*` : 
      When `sudo netplan apply`, all files under `/etc/netplan/` are read and network is configured.
      - `01-installer-config.yaml` : 
        ![01-installer-config.yaml](image-1.png)
    > ❗️When static IP is set, NAT will no longer work. To use NAT : 
    > 1. remove `01-installer-config.yaml` from `/etc/netplan/`
    > 2. re-run `sudo netplan apply`
    > 3. `reboot`

- Environment Setup : 

  - **openssh-server** : for host OS to ssh into the VM to run commands.  

    ```
    sudo apt update
    sudo apt install openssh-server

    # start ssh service
    sudo systemctl start ssh

    # enable ssh to start on boot
    sudo systemctl enable ssh

    # check status of SSH
    sudo systemctl status ssh
    ```
  - **stress-ng** : must be the latest version (v0.18.01)
    ```
    sudo apt install wget build-essential
    wget https://github.com/ColinIanKing/stress-ng/tarball/V0.18.01 -O stress-ng-0.18.01.tar.gz
    tar -xvf stress-ng-0.18.01.tar.gz
    cd ColinIanKing-stress-ng-*
    make
    sudo make install
    ```
  - **other packages** : 
    ```
    sudo apt install golang vim git python3-pip wget auditd stress-ng screen apparmor
    pip3 install flask psutil
    echo 'export PATH=$PATH:/home/{vm name}/.local/bin' >> ~/.bashrc && source ~/.bashrc
    ```

- **Benign_Transform_vm** : static IP `192.168.56.146`
