# EEG 去噪与脑信号深度学习论文质量验收集

围绕 EEG 去噪、伪迹移除、生成模型、脑信号基础模型、经典信号处理和评测数据集。

- 收录数量：33
- 核验规则：Crossref DOI 直接记录或 arXiv 原始记录；标题相似度门槛 0.92。
- 生成时间：2026-10-21T05:29:27.984999+00:00
- 说明：Crossref 被引次数仅作影响力参考；近期论文可能尚未积累引用。

## 核心去噪

### 1. EEGdenoiseNet: a benchmark dataset for deep learning solutions of EEG denoising

- 作者：Haoming Zhang, Mingqi Zhao, Chen Wei, Dante Mantini, et al.
- 年份：2021
- 来源：Journal of Neural Engineering
- 链接：https://doi.org/10.1088/1741-2552/ac2bf8
- DOI：10.1088/1741-2552/ac2bf8
- 层级/质量信号：领域基准论文；Crossref 被引 258
- 核心内容：提出 EEGdenoiseNet 基准数据集和深度学习去噪评测框架，为 EEG 去噪模型提供统一数据与指标基础。
- 项目价值：可直接用于项目的去噪模型评测、数据构造和 SNR/CC/RMSE 类指标回归。
- 核验状态：verified_crossref_doi_record

### 2. AnEEG: leveraging deep learning for effective artifact removal in EEG data

- 作者：Bhabesh Kalita, Nabamita Deb, Daisy Das
- 年份：2024
- 来源：Scientific Reports
- 链接：https://doi.org/10.1038/s41598-024-75091-z
- DOI：10.1038/s41598-024-75091-z
- 层级/质量信号：同行评议期刊；Crossref 被引 58
- 核心内容：提出面向 EEG 自动伪迹移除的深度学习框架，并在真实 EEG 数据处理流程中评估其有效性。
- 项目价值：可作为端到端伪迹移除架构和工程化部署的近期对照方案。
- 核验状态：verified_crossref_doi_record

### 3. Frequency Information Enhanced Deep EEG Denoising Network for Ocular Artifact Removal

- 作者：Jin Yin, Aiping Liu, Chang Li, Ruobing Qian, et al.
- 年份：2022
- 来源：IEEE Sensors Journal
- 链接：https://doi.org/10.1109/jsen.2022.3209805
- DOI：10.1109/jsen.2022.3209805
- 层级/质量信号：IEEE 期刊；Crossref 被引 32
- 核心内容：利用频域信息增强深度网络，实现眼电伪迹去除，强调频谱特征对 EEG 去噪的补充作用。
- 项目价值：支持项目引入时频联合建模、频带约束和眼电伪迹专门分支。
- 核验状态：verified_crossref_doi_record

### 4. Automatic Electroencephalogram Artifact Removal Using Deep Convolutional Neural Networks

- 作者：Fabio Lopes, Adriana Leal, Julio Medeiros, Mauro F. Pinto, et al.
- 年份：2021
- 来源：IEEE Access
- 链接：https://doi.org/10.1109/access.2021.3125728
- DOI：10.1109/access.2021.3125728
- 层级/质量信号：IEEE Access；Crossref 被引 51
- 核心内容：使用深度卷积神经网络实现自动 EEG 伪迹移除，并给出端到端训练与重建流程。
- 项目价值：可作为 CNN 基线、消融实验对象和轻量化模型设计参考。
- 核验状态：verified_crossref_doi_record

### 5. A Convolutional Neural Network for the Removal of Simultaneous Ocular and Myogenic Artifacts from EEG Signals

- 作者：Maryam Azhar, Tamoor Shafique, Anas Amjad
- 年份：2024
- 来源：Electronics
- 链接：https://doi.org/10.3390/electronics13224576
- DOI：10.3390/electronics13224576
- 层级/质量信号：同行评议期刊；Crossref 被引 10
- 核心内容：设计卷积神经网络同时去除 EEG 中的眼电和肌电伪迹，关注复合伪迹场景。
- 项目价值：适合用于项目多伪迹混合去除实验和单任务/多任务架构比较。
- 核验状态：verified_crossref_doi_record

### 6. End-to-end EEG artifact removal method via nested generative adversarial network

- 作者：Tianqi Yang, Nan Hu, Shengsheng Cai, Dongyang Xu
- 年份：2025
- 来源：Biomedical Physics &amp; Engineering Express
- 链接：https://doi.org/10.1088/2057-1976/ae1a8c
- DOI：10.1088/2057-1976/ae1a8c
- 层级/质量信号：近期同行评议期刊；Crossref 被引 2
- 核心内容：提出嵌套生成对抗网络实现端到端 EEG 伪迹移除，强调生成与判别模块的协同训练。
- 项目价值：直接支撑项目的双判别器/GAN 去噪架构设计和近期基线比较。
- 核验状态：verified_crossref_doi_record

### 7. EEG Artifact Removal by Bayesian Deep Learning &amp; ICA

- 作者：Sangmin S. Lee, Kiwon Lee, Guiyeom Kang
- 年份：2020
- 来源：2020 42nd Annual International Conference of the IEEE Engineering in Medicine &amp; Biology Society (EMBC)
- 链接：https://doi.org/10.1109/embc44109.2020.9175785
- DOI：10.1109/embc44109.2020.9175785
- 层级/质量信号：IEEE EMBC；Crossref 被引 16
- 核心内容：将贝叶斯深度学习与独立成分分析结合，用于 EEG 伪迹识别和去除。
- 项目价值：为项目提供不确定性建模与经典 ICA 先验融合的思路。
- 核验状态：verified_crossref_doi_record

### 8. Motion Artifact Removal from EEG Signals Using the Motion-Net Deep Learning Algorithm

- 作者：Ege Kibrislioglu, Taous-Meriem Laleg-Kirati
- 年份：2026
- 来源：Biomedical Signal Processing and Control
- 链接：https://doi.org/10.1016/j.bspc.2025.108877
- DOI：10.1016/j.bspc.2025.108877
- 层级/质量信号：同行评议期刊；Crossref 被引 4
- 核心内容：提出 Motion-Net 深度学习算法，专门处理 EEG 中的运动伪迹。
- 项目价值：补充项目对运动伪迹、可穿戴 EEG 和非平稳噪声场景的覆盖。
- 核验状态：verified_crossref_doi_record

### 9. FASTER: Fully Automated Statistical Thresholding for EEG artifact Rejection

- 作者：H. Nolan, R. Whelan, R.B. Reilly
- 年份：2010
- 来源：Journal of Neuroscience Methods
- 链接：https://doi.org/10.1016/j.jneumeth.2010.07.015
- DOI：10.1016/j.jneumeth.2010.07.015
- 层级/质量信号：经典方法；Crossref 被引 1092
- 核心内容：提出 FASTER 全自动统计阈值 EEG 伪迹拒绝方法，是传统伪迹检测的重要基线。
- 项目价值：可作为深度模型对照，并用于自动标注、阈值化伪迹检测和数据清洗。
- 核验状态：verified_crossref_doi_record

### 10. ADJUST: An automatic EEG artifact detector based on the joint use of spatial and temporal features

- 作者：Andrea Mognon, Jorge Jovicich, Lorenzo Bruzzone, Marco Buiatti
- 年份：2011
- 来源：Psychophysiology
- 链接：https://doi.org/10.1111/j.1469-8986.2010.01061.x
- DOI：10.1111/j.1469-8986.2010.01061.x
- 层级/质量信号：经典方法；Crossref 被引 1187
- 核心内容：ADJUST 利用空间和时序联合特征自动检测 EEG 伪迹成分。
- 项目价值：为 ICA 成分筛选、伪迹检测器和混合式预处理流程提供经典参照。
- 核验状态：verified_crossref_doi_record

## 生成模型与数据增强

### 1. Data Augmentation for EEG-Based Emotion Recognition Using Generative Adversarial Networks

- 作者：Guangcheng Bao, Bin Yan, Li Tong, Jun Shu, et al.
- 年份：2021
- 来源：Frontiers in Computational Neuroscience
- 链接：https://doi.org/10.3389/fncom.2021.723843
- DOI：10.3389/fncom.2021.723843
- 层级/质量信号：同行评议期刊；Crossref 被引 54
- 核心内容：使用生成对抗网络扩充 EEG 情感识别数据，并评估合成样本对分类性能的影响。
- 项目价值：支持项目研究小样本 EEG 数据增强、生成质量评估和下游任务收益。
- 核验状态：verified_crossref_doi_record

### 2. Residual-Enhanced VAE-GAN for EEG Data Augmentation in Motor Imagery Classification

- 作者：Haiyuan Tang, Wenshan Zhao
- 年份：2025
- 来源：2025 IEEE International Conference on Consumer Electronics - Taiwan (ICCE-Taiwan)
- 链接：https://doi.org/10.1109/icce-taiwan66881.2025.11208108
- DOI：10.1109/icce-taiwan66881.2025.11208108
- 层级/质量信号：IEEE 会议；Crossref 被引 1
- 核心内容：提出残差增强 VAE-GAN，用于运动想象 EEG 数据增强和分类。
- 项目价值：为项目提供 VAE-GAN 混合生成架构和类别不平衡处理参考。
- 核验状态：verified_crossref_doi_record

## 深度架构与脑基础模型

### 1. EEGNet: a compact convolutional neural network for EEG-based brain–computer interfaces

- 作者：Vernon J Lawhern, Amelia J Solon, Nicholas R Waytowich, Stephen M Gordon, et al.
- 年份：2018
- 来源：Journal of Neural Engineering
- 链接：https://doi.org/10.1088/1741-2552/aace8c
- DOI：10.1088/1741-2552/aace8c
- 层级/质量信号：领域经典；Crossref 被引 4959
- 核心内容：提出紧凑型 EEGNet 卷积网络，以深度可分离卷积处理多通道 EEG 分类任务。
- 项目价值：是轻量 EEG 编码器、参数量控制和 CNN 基线的核心参考。
- 核验状态：verified_crossref_doi_record

### 2. EEG Conformer: Convolutional Transformer for EEG Decoding and Visualization

- 作者：Yonghao Song, Qingqing Zheng, Bingchuan Liu, Xiaorong Gao
- 年份：2023
- 来源：IEEE Transactions on Neural Systems and Rehabilitation Engineering
- 链接：https://doi.org/10.1109/tnsre.2022.3230250
- DOI：10.1109/tnsre.2022.3230250
- 层级/质量信号：IEEE 期刊；Crossref 被引 1176
- 核心内容：将卷积局部特征与 Transformer 自注意力结合，用于 EEG 解码和可视化。
- 项目价值：支撑项目在 U-Net/CNN 编码器中引入注意力或混合卷积 Transformer。
- 核验状态：verified_crossref_doi_record

### 3. BIOT: Biosignal Transformer for Cross-data Learning in the Wild

- 作者：Chaoqi Yang, M Westover, Jimeng Sun
- 年份：2023
- 来源：Advances in Neural Information Processing Systems 36
- 链接：https://doi.org/10.52202/075280-3420
- DOI：10.52202/075280-3420
- 层级/质量信号：NeurIPS 2023；Crossref 被引 66
- 核心内容：提出面向多类生物信号的 Transformer，实现跨数据集和跨任务统一建模。
- 项目价值：为项目设计跨数据集 EEG 预训练、迁移学习和通道异构处理提供方案。
- 核验状态：verified_crossref_doi_record

### 4. EEGPT: Pretrained Transformer for Universal and Reliable Representation of EEG Signals

- 作者：Guagnyu Wang, Wenchao Liu, Yuhong He, Cong Xu, et al.
- 年份：2024
- 来源：Advances in Neural Information Processing Systems 37
- 链接：https://doi.org/10.52202/079017-1239
- DOI：10.52202/079017-1239
- 层级/质量信号：NeurIPS 2024；Crossref 被引 59
- 核心内容：提出通用 EEG 预训练 Transformer，学习可迁移的 EEG 表征并适配多种下游任务。
- 项目价值：支持项目评估自监督预训练、表征迁移和少量标注微调。
- 核验状态：verified_crossref_doi_record

### 5. Large Brain Model for Learning Generic Representations with Tremendous EEG Data in BCI

- 作者：Wei-Bang Jiang, Li-Ming Zhao, Bao-Liang Lu
- 年份：2024
- 来源：The Twelfth International Conference on Learning Representations, 2024
- 链接：https://arxiv.org/abs/2405.18765
- DOI：(arXiv 无 DOI)
- 层级/质量信号：ICLR 2024
- 核心内容：LaBraM 将 EEG 切成通道 patch，通过神经码本预测进行大规模无监督预训练。
- 项目价值：为跨数据集 EEG 基础模型、掩码预训练和通道异构建模提供核心架构参考。
- 核验状态：verified_arxiv_record

### 6. BrainBERT: Self-supervised representation learning for intracranial recordings

- 作者：Christopher Wang, Vighnesh Subramaniam, Adam Uri Yaari, Gabriel Kreiman, et al.
- 年份：2023
- 来源：The Eleventh International Conference on Learning Representations (ICLR 2023)
- 链接：https://arxiv.org/abs/2302.14367
- DOI：(arXiv 无 DOI)
- 层级/质量信号：ICLR 2023
- 核心内容：BrainBERT 通过掩码自监督学习颅内神经记录的通用 Transformer 表征。
- 项目价值：支持项目借鉴自监督掩码任务和神经信号表征学习方法。
- 核验状态：verified_arxiv_record

## 应用、数据集与评测

### 1. EEG Emotion Recognition Using Dynamical Graph Convolutional Neural Networks

- 作者：Tengfei Song, Wenming Zheng, Peng Song, Zhen Cui
- 年份：2020
- 来源：IEEE Transactions on Affective Computing
- 链接：https://doi.org/10.1109/taffc.2018.2817622
- DOI：10.1109/taffc.2018.2817622
- 层级/质量信号：IEEE 期刊；Crossref 被引 1483
- 核心内容：使用动态图卷积网络建模 EEG 通道关系并完成情感识别。
- 项目价值：为项目引入电极空间图、动态连接和下游任务验证提供参考。
- 核验状态：verified_crossref_doi_record

### 2. An Attention-Based Deep Learning Approach for Sleep Stage Classification With Single-Channel EEG

- 作者：Emadeldeen Eldele, Zhenghua Chen, Chengyu Liu, Min Wu, et al.
- 年份：2021
- 来源：IEEE Transactions on Neural Systems and Rehabilitation Engineering
- 链接：https://doi.org/10.1109/tnsre.2021.3076234
- DOI：10.1109/tnsre.2021.3076234
- 层级/质量信号：IEEE 期刊；Crossref 被引 705
- 核心内容：面向单通道 EEG 睡眠分期提出注意力深度学习模型，强调可解释性和时序依赖。
- 项目价值：可用于评估去噪后信号对睡眠分期等下游任务的可迁移收益。
- 核验状态：verified_crossref_doi_record

### 3. EEG-Based Emotion Recognition Using Regularized Graph Neural Networks

- 作者：Peixiang Zhong, Di Wang, Chunyan Miao
- 年份：2022
- 来源：IEEE Transactions on Affective Computing
- 链接：https://doi.org/10.1109/taffc.2020.2994159
- DOI：10.1109/taffc.2020.2994159
- 层级/质量信号：IEEE 期刊；Crossref 被引 813
- 核心内容：提出正则化图神经网络建模 EEG 电极关系，用于跨被试情感识别。
- 项目价值：为项目处理跨被试泛化、图结构先验和下游任务稳健性提供参考。
- 核验状态：verified_crossref_doi_record

### 4. The Temple University Hospital EEG Data Corpus

- 作者：Iyad Obeid, Joseph Picone
- 年份：2016
- 来源：Frontiers in Neuroscience
- 链接：https://doi.org/10.3389/fnins.2016.00196
- DOI：10.3389/fnins.2016.00196
- 层级/质量信号：公开数据集；Crossref 被引 514
- 核心内容：介绍 Temple University Hospital EEG Corpus 大规模临床 EEG 数据资源。
- 项目价值：可作为临床分布、跨中心泛化和真实噪声场景的数据来源。
- 核验状态：verified_crossref_doi_record

### 5. MOABB: trustworthy algorithm benchmarking for BCIs

- 作者：Vinay Jayaram, Alexandre Barachant
- 年份：2018
- 来源：Journal of Neural Engineering
- 链接：https://doi.org/10.1088/1741-2552/aadea0
- DOI：10.1088/1741-2552/aadea0
- 层级/质量信号：可复现基准；Crossref 被引 227
- 核心内容：MOABB 提供脑机接口算法的统一数据集接口、评测协议和可重复基准。
- 项目价值：支持项目建立公平比较、预处理一致性和可重复 BCI 回归测试。
- 核验状态：verified_crossref_doi_record

### 6. The impact of the MIT-BIH Arrhythmia Database

- 作者：G.B. Moody, R.G. Mark
- 年份：2001
- 来源：IEEE Engineering in Medicine and Biology Magazine
- 链接：https://doi.org/10.1109/51.932724
- DOI：10.1109/51.932724
- 层级/质量信号：经典公开数据集；Crossref 被引 4311
- 核心内容：介绍 MIT-BIH Arrhythmia Database 的构成、影响和心律失常研究用途。
- 项目价值：对应当前论文使用的心电数据评估场景，可用于跨模态噪声和信号保真实验。
- 核验状态：verified_crossref_doi_record

## 经典信号处理

### 1. Independent component analysis: algorithms and applications

- 作者：A. Hyvärinen, E. Oja
- 年份：2000
- 来源：Neural Networks
- 链接：https://doi.org/10.1016/s0893-6080(00)00026-5
- DOI：10.1016/s0893-6080(00)00026-5
- 层级/质量信号：经典奠基论文；Crossref 被引 7153
- 核心内容：系统阐述独立成分分析的基本模型、目标函数和经典算法。
- 项目价值：为 ICA 伪迹分离、源信号独立性和预处理方法提供理论基础。
- 核验状态：verified_crossref_doi_record

### 2. A blind source separation technique using second-order statistics

- 作者：A. Belouchrani, K. Abed-Meraim, J.-F. Cardoso, E. Moulines
- 年份：1997
- 来源：IEEE Transactions on Signal Processing
- 链接：https://doi.org/10.1109/78.554307
- DOI：10.1109/78.554307
- 层级/质量信号：经典奠基论文；Crossref 被引 2299
- 核心内容：提出基于二阶统计量的 SOBI 盲源分离方法。
- 项目价值：支持项目比较 ICA、SOBI 和深度去噪在源分离与伪迹抑制上的差异。
- 核验状态：verified_crossref_doi_record

### 3. The empirical mode decomposition and the Hilbert spectrum for nonlinear and non-stationary time series analysis

- 作者：Norden E. Huang, Zheng Shen, Steven R. Long, Manli C. Wu, et al.
- 年份：1998
- 来源：Proceedings of the Royal Society of London. Series A: Mathematical, Physical and Engineering Sciences
- 链接：https://doi.org/10.1098/rspa.1998.0193
- DOI：10.1098/rspa.1998.0193
- 层级/质量信号：经典奠基论文；Crossref 被引 20284
- 核心内容：提出经验模态分解和 Hilbert 谱，用于非线性和非平稳信号分析。
- 项目价值：为项目解释传统时频分解、非平稳伪迹处理和基线方法提供依据。
- 核验状态：verified_crossref_doi_record

## 综述与方法迁移

### 1. Deep learning-based electroencephalography analysis: a systematic review

- 作者：Yannick Roy, Hubert Banville, Isabela Albuquerque, Alexandre Gramfort, et al.
- 年份：2019
- 来源：Journal of Neural Engineering
- 链接：https://doi.org/10.1088/1741-2552/ab260c
- DOI：10.1088/1741-2552/ab260c
- 层级/质量信号：领域综述；Crossref 被引 1442
- 核心内容：系统回顾深度学习在 EEG 分析中的任务、模型、数据与评测方法。
- 项目价值：用于定位项目在 EEG 深度学习研究中的方法位置和评测缺口。
- 核验状态：verified_crossref_doi_record

### 2. Deep learning techniques for classification of electroencephalogram (EEG) motor imagery (MI) signals: a review

- 作者：Hamdi Altaheri, Ghulam Muhammad, Mansour Alsulaiman, Syed Umar Amin, et al.
- 年份：2021
- 来源：Neural Computing and Applications
- 链接：https://doi.org/10.1007/s00521-021-06352-5
- DOI：10.1007/s00521-021-06352-5
- 层级/质量信号：领域综述；Crossref 被引 555
- 核心内容：综述 EEG 运动想象分类中的深度学习技术、数据挑战和模型选择。
- 项目价值：支持项目选择下游分类任务、比较编码器结构并设计迁移实验。
- 核验状态：verified_crossref_doi_record

### 3. Advanced Signal Processing and Machine/Deep Learning Approaches on a Preprocessing Block for EEG Artifact Removal: A Comprehensive Review

- 作者：Said Agounad, Ousama Tarahi, Mustapha Moufassih, Soukaina Hamou, et al.
- 年份：2024
- 来源：Circuits, Systems, and Signal Processing
- 链接：https://doi.org/10.1007/s00034-024-02936-3
- DOI：10.1007/s00034-024-02936-3
- 层级/质量信号：近期综述；Crossref 被引 27
- 核心内容：综述 EEG 伪迹移除预处理中的传统信号处理、机器学习和深度学习方法。
- 项目价值：可直接用于项目相关工作梳理、方法分类和基线选择。
- 核验状态：verified_crossref_doi_record

### 4. A review of motion related EEG artifact removal techniques

- 作者：K. Cao, Y. Guo, Steven W. Su
- 年份：2015
- 来源：2015 9th International Conference on Sensing Technology (ICST)
- 链接：https://doi.org/10.1109/icsenst.2015.7438469
- DOI：10.1109/icsenst.2015.7438469
- 层级/质量信号：专题综述；Crossref 被引 8
- 核心内容：回顾 EEG 运动相关伪迹的检测与去除技术。
- 项目价值：补充项目对可穿戴、运动场景和复杂非平稳伪迹的认识。
- 核验状态：verified_crossref_doi_record

### 5. Deep learning on image denoising: An overview

- 作者：Chunwei Tian, Lunke Fei, Wenxian Zheng, Yong Xu, et al.
- 年份：2020
- 来源：Neural Networks
- 链接：https://doi.org/10.1016/j.neunet.2020.07.025
- DOI：10.1016/j.neunet.2020.07.025
- 层级/质量信号：方法综述；Crossref 被引 1012
- 核心内容：综述图像去噪中的深度网络架构、监督策略和评测方法。
- 项目价值：为项目迁移 CNN、注意力、残差学习和去噪训练策略提供方法论参考。
- 核验状态：verified_crossref_doi_record

### 6. Attention-guided CNN for image denoising

- 作者：Chunwei Tian, Yong Xu, Zuoyong Li, Wangmeng Zuo, et al.
- 年份：2020
- 来源：Neural Networks
- 链接：https://doi.org/10.1016/j.neunet.2019.12.024
- DOI：10.1016/j.neunet.2019.12.024
- 层级/质量信号：方法论文；Crossref 被引 660
- 核心内容：提出注意力引导的 CNN 图像去噪方法，利用注意力机制选择关键特征。
- 项目价值：为项目设计通道/空间注意力和残差去噪模块提供直接参考。
- 核验状态：verified_crossref_doi_record
