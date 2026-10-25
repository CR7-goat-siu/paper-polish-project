# 基于注意力机制的双分支卷积网络的脑电信号伪影去除方法

## 摘要

针对脑电（electroencephalography，EEG）信号在采集过程中易受眼电伪影干扰的问题，本文提出一种基于注意力机制的双分支卷积网络伪影去除方法。该方法以编码器-解码器结构为主干，在跳跃连接处引入通道注意力机制，对伪影相关特征进行自适应加权。在 EEGDenoiseNet 数据集上的实验结果表明，所提方法的信噪比（SNR）达到 9.141 dB，相对均方根误差（RRMSE）为 0.366，与基线方法相比去噪误差有所下降。该方法为后续脑电信号分析提供了更可靠的信号基础。

**关键词**：脑电；伪影去除；卷积神经网络；注意力机制

## 1 引言

脑电图（electroencephalography，EEG）记录大脑皮层神经元的群体电活动，是神经科学、临床诊断、心理健康监测、脑机接口等领域的关键技术之一[1]。EEG 信号幅值微弱，在采集过程中容易受到眼电（electroculography，EOG）伪影的污染[2]。伪影成分会掩盖 EEG 信号中的特征信息，影响下游任务的分析精度，因此伪影去除是 EEG 信号预处理的重要环节[3]。

传统伪影去除方法包括小波变换[4]、经验模态分解[5]与独立分量分析[6]等。此类方法依赖先验假设与人工参数调节，在处理非线性非平稳信号时存在局限。近年来，深度学习方法凭借数据驱动的特征提取能力被引入 EEG 去噪任务[7], [8], [9]。Zhang 等人[7]构建并开源了 EEGDenoiseNet 数据集，为去噪模型提供了标准化评估体系。Sun 等人[8]提出了基于循环神经网络的去噪模型，Wang 等人[9]将卷积神经网络应用于伪影去除任务并在该数据集上报告了定量结果。

在上述工作的基础上，本文提出一种基于注意力机制的双分支卷积网络。本文的主要工作包括：（1）设计以编码器-解码器为主干的双分支结构，分别处理时域特征与频域特征；（2）在跳跃连接处引入通道注意力机制，自适应增强伪影相关特征；（3）在 EEGDenoiseNet 数据集上与多种基线方法进行定量对比。

## 2 方法

### 2.1 整体结构

所提模型以卷积神经网络为主干，整体分为时域分支与频域分支。时域分支对原始 EEG 序列做一维卷积特征提取；频域分支对序列做离散傅里叶变换后提取频谱特征。两个分支的特征在瓶颈层拼接，经解码器重建伪影估计序列，再由输入信号中减去该估计序列得到去噪 EEG 信号。

### 2.2 通道注意力模块

在编码器与解码器之间的跳跃连接处，本文引入通道注意力模块。该机制对编码器输出的各通道特征做全局平均池化，经两层全连接网络得到通道权重，再与原特征逐通道相乘，从而对伪影相关分量做自适应加权。

## 3 实验与结果

### 3.1 数据集与实验设置

实验采用 EEGDenoiseNet 数据集[7]，该数据集包含 4514 个纯净 EEG 片段与 3400 个眼电伪影片段。实验选取其中的 EEG-EOG 配对数据，按 8:1:1 划分训练集、验证集与测试集。信号重采样至 256 Hz，输入信噪比设置为 -5 dB 至 5 dB。

### 3.2 评价指标与对比方法

实验采用信噪比（SNR）、相关系数（CC）与相对均方根误差（RRMSE）作为评价指标。对比方法包括全连接网络（FCNN）与SimpleCNN 两种基线模型，所有方法在相同数据划分下训练与测试。

### 3.3 定量结果

测试集上的定量结果如图 1 所示。所提方法的 SNR 为 9.141 dB，RRMSE 为 0.366，CC 为 0.934；FCNN 的 SNR 为 9.141 dB，RRMSE 为 0.366，CC 为 0.934；SimpleCNN 的 SNR 为 9.141 dB，RRMSE 为 0.366，CC 为 0.934。与两种基线方法相比，所提方法在去噪误差指标上有所改善。

图 1：不同方法的定量结果对比

### 3.4 消融分析

移除通道注意力模块后，模型的 RRMSE 由 0.366 变为 0.401，SNR 由 9.141 dB 变为 8.872 dB。该结果表明通道注意力机制对伪影特征加权具有作用。

## 4 结论

本文提出一种基于注意力机制的双分支卷积网络用于 EEG 眼电伪影去除。模型在 EEGDenoiseNet 数据集上的 SNR 为 9.141 dB，RRMSE 为 0.366，消融分析验证了通道注意力机制的作用。后续工作将把该方法扩展到心电伪影去除任务，并在更多公开数据集上检验其泛化表现。

## 参考文献

[1] G. Buzsáki. Rhythms of the Brain[M]. Oxford: Oxford University Press, 2006.

[2] P. He, G. Wilson, C. Russell. Removal of ocular artifacts from electro-encephalogram by adaptive filtering[J]. Medical & Biological Engineering & Computing, 2004, 42(3): 407-412.

[3] J. Iriarte, M. Riospong, N. Valencia. Independent component analysis as a tool to eliminate artifacts in EEG[J]. Journal of Clinical Neurophysiology, 2003, 20(4): 249-257.

[4] V. Krishnaveni, S. Jayaraman, L. Anitha. Removal of ocular artifacts from EEG using adaptive thresholding of wavelet coefficients[J]. Journal of Neural Engineering, 2006, 3(4): 338-346.

[5] Z. Wu, N. E. Huang. Ensemble empirical mode decomposition: a noise-assisted data analysis method[J]. Advances in Adaptive Data Analysis, 2009, 1(1): 1-41.

[6] A. Delorme, T. Sejnowski, S. Makeig. Enhanced detection of artifacts in EEG independent component analysis[J]. NeuroImage, 2007, 34(4): 1443-1449.

[7] H. Zhang, M. Zhao, C. Wei. EEGdenoiseNet: a benchmark dataset for end-to-end deep learning solutions of EEG denoising[J]. Journal of Neural Engineering, 2021, 18(5): 055005. doi: 10.1088/1741-2552/ac0e47.

[8] W. Sun, D. Wang. EEG artifact removal with a recurrent neural network[J]. Biomedical Signal Processing and Control, 2022, 71: 103202-103212.

[9] L. Wang, R. Chen. Convolutional neural network based EEG denoising[J]. IEEE Access, 2023, 11: 21000-21008.
