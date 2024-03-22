import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt
import seaborn as sns
import torch
import torch.nn as nn
import torch.nn.functional as nnF
import torchdiffeq
import time

device = "cpu"
torch.manual_seed(0)
np.random.seed(0)

mpl.use("Qt5Agg")
plt.rcParams["agg.path.chunksize"] = 10000
plt.rc("text", usetex=True)
plt.rcParams["font.family"] = "Times New Roman"
plt.rcParams["text.latex.preamble"] = r"\usepackage{amsmath} \boldmath"

######################################
########## Data Generation ###########
######################################
F = 8
sigma = 0.5

I = 36
Lt = 300
dt = 0.001
Nt = int(Lt/dt) + 1
t = np.linspace(0, Lt, Nt)
u = np.zeros((Nt, I))

for n in range(Nt-1):
    for i in range(I):
        u_dot = -u[n, i] + u[n,(i+1)%I]*u[n,i-1] - u[n,i-2]*u[n,i-1] + F
        u[n+1, i] = u[n, i] + u_dot*dt + sigma*np.sqrt(dt)*np.random.randn()


# Sub-sampling
u = u[::10]
dt = 0.01
Nt = int(Lt/dt) + 1
t = np.linspace(0, Lt, Nt)
u_dot = np.diff(u, axis=0)/dt

# Split data in to train and test
u_dot = torch.tensor(u_dot, dtype=torch.float32)
u = torch.tensor(u[:-1], dtype=torch.float32)
t = torch.tensor(t[:-1], dtype=torch.float32)

Ntrain = 10000
Ntest = 20000
train_u = u[:Ntrain]
train_u_dot = u_dot[:Ntrain]
train_t = t[:Ntrain]
test_u = u[-Ntest:]
test_u_dot = u_dot[-Ntest:]
test_t = t[-Ntest:]


# Indices of u1 and u2
indices_u1 = np.array([i for i in range(36) if i % 3 != 2])
indices_u2 = np.array([i for i in range(36) if i % 3 == 2])
dim_u1 = len(indices_u1)
dim_u2 = len(indices_u2)

################################
########## CG-Filter ###########
################################
u = test_u.numpy()
Nt = u.shape[0]
indices_u1 = np.array([i for i in range(36) if i % 3 != 2])
indices_u2 = np.array([i for i in range(36) if i % 3 == 2])

dim_u1 = len(indices_u1)
dim_u2 = len(indices_u2)
mu_trace = np.zeros((Nt, dim_u2, 1))
R_trace = np.zeros((Nt, dim_u2, dim_u2))
mu_trace[0] = np.zeros((dim_u2, 1))
R_trace[0] = np.eye(dim_u2)*0.01
mu0 = mu_trace[0]
R0 = R_trace[0]

for n in range(1, Nt):
    du1 = (u[n, indices_u1] - u[n-1, indices_u1]).reshape(-1, 1)

    f1 = F-u[n-1, indices_u1].reshape(-1, 1)
    g1 = np.zeros((dim_u1, dim_u2))
    for j in range(dim_u2):
        g1[np.arange(2*j+1, 2*j+4)%dim_u1, j] = [u[n-1, 3*j], u[n-1, (3*j+4)%I]-u[n-1, 3*j+1], -u[n-1, (3*j+3)%I]]
    s1 = np.diag([sigma]*dim_u1)
    f2 = np.zeros((dim_u2, 1))
    for i in range(dim_u2):
        f2[i] = F + (u[n-1, (3*i+3)%I] - u[n-1, 3*i]) * u[n-1, 3*i+1]
    g2 = np.diag([-1]*dim_u2)
    s2 = np.diag([sigma]*dim_u2)
    invs1os1 = np.linalg.inv(s1@s1.T)
    s2os2 = s2@s2.T

    mu1 = mu0 + (f2+g2@mu0)*dt + (R0@g1.T) @ invs1os1 @ (du1 -(f1+g1@mu0)*dt)
    R1 = R0 + ( g2@R0 + R0@g2.T + s2os2 - R0@g1.T@ invs1os1 @ g1@R0 )*dt
    mu_trace[n] = mu1
    R_trace[n] = R1
    mu0 = mu1
    R0 = R1

np.mean( (u[:, indices_u2] - mu_trace.squeeze(2) )**2 )


def avg_neg_log_likehood(x, mu, R):
    # x, mu are in matrix form, e.g. (t, x, 1)
    d = x.shape[1]
    neg_log_likehood = 1/2*(d*np.log(2*np.pi) + torch.log(torch.linalg.det(R)) + ((x-mu).permute(0,2,1)@torch.linalg.inv(R)@(x-mu)).flatten())
    return torch.mean(neg_log_likehood)
avg_neg_log_likehood(torch.tensor(u[:,indices_u2].reshape(20000, 12 ,1)),
                     torch.tensor(mu_trace),
                     torch.tensor(R_trace))


# CGF for models

class RegModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.reg = nn.Parameter(torch.randn(2))
    def forward(self, t, u):
        out = u*self.reg[1] + self.reg[0]
        return out

class UnitNet1(nn.Module):
    def __init__(self, input_size=3, output_size=3):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(input_size, 3), nn.ReLU(),
                                 nn.Linear(3, 6), nn.ReLU(),
                                 nn.Linear(6, output_size))

    def forward(self, x):
        out = self.net(x)
        return out
class UnitNet2(nn.Module):
    def __init__(self, input_size=3, output_size=3):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(input_size, 3), nn.ReLU(),
                                 nn.Linear(3, 6), nn.ReLU(),
                                 nn.Linear(6, output_size))

    def forward(self, x):
        out = self.net(x)
        return out
class UnitNet3(nn.Module):
    def __init__(self, input_size=4, output_size=2):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(input_size, 4), nn.ReLU(),
                                 nn.Linear(4, 6), nn.ReLU(),
                                 nn.Linear(6, output_size))

    def forward(self, x):
        out = self.net(x)
        return out

class CGNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.unitnet1 = UnitNet1()
        self.unitnet2 = UnitNet2()
        self.unitnet3 = UnitNet3()

    def forward(self, u1):
        N, dim_u1 = u1.shape
        x1 = torch.stack([u1[:, [2*i-1, 2*i, 2*i+1]] for i in range(12)], dim=1)
        x2 = torch.stack([u1[:, [2*i, 2*i+1, (2*i+2)%dim_u1]] for i in range(12)], dim=1)
        x3 = torch.stack([u1[:, [2*i, 2*i+1, (2*i+2)%dim_u1, (2*i+3)%dim_u1]] for i in range(12)], dim=1)
        out1 = self.unitnet1(x1)
        out2 = self.unitnet2(x2)
        out3 = self.unitnet3(x3)
        return (out1, out2, out3)

class MixModel(nn.Module):
    def __init__(self, cgnn):
        super().__init__()
        self.outreg = None
        self.outnet = None
        self.out = None
        self.reg = nn.Parameter(torch.randn(2))
        self.net = cgnn

    def forward(self, t, u):
        self.outreg = u*self.reg[1] + self.reg[0]

        indices_u1 = np.array([i for i in range(36) if i % 3 != 2])
        u1 = u[:, indices_u1]
        self.outnet = self.net(u1)

        self.out = torch.zeros_like(u)
        self.out[:, ::3] = self.outreg[:, ::3] + self.outnet[0][:, :, 0] + self.outnet[0][:,:, 1]*u[:, torch.arange(-1,33,3)] + self.outnet[0][:, :, 2]*u[:, torch.arange(2,36,3)]
        self.out[:, 1::3] = self.outreg[:, 1::3] + self.outnet[1][:, :, 0] + self.outnet[1][:,:, 1]*u[:, torch.arange(-1,33,3)] + self.outnet[1][:, :, 2]*u[:, torch.arange(2,36,3)]
        self.out[:, 2::3] = self.outreg[:, 2::3] + self.outnet[2][:, :, 0] + self.outnet[2][:, :, 1]*u[:,torch.arange(2,36,3)]
        return self.out

def CGFilter_RegModel(regmodel, u1, mu0, R0, cut_point, sigma_lst):
    # u1, mu0 are in col-matrix form, e.g. (t, x, 1)
    device = u1.device
    sigma_tsr = torch.tensor(sigma_lst)

    indices_u1 = np.array([i for i in range(36) if i % 3 != 2])
    indices_u2 = np.array([i for i in range(36) if i % 3 == 2])

    dim_u1 = len(indices_u1)
    dim_u2 = len(indices_u2)
    dim_u = dim_u1 + dim_u2

    FF = regmodel.reg[0]
    c = regmodel.reg[1]

    Nt = u1.shape[0]
    mu_trace = torch.zeros((Nt, dim_u2, 1)).to(device)
    R_trace = torch.zeros((Nt, dim_u2, dim_u2)).to(device)
    mu_trace[0] = mu0
    R_trace[0] = R0
    for n in range(1, Nt):
        du1 = u1[n] - u1[n-1]

        f1 = c * u1[n-1] + FF
        g1 = torch.zeros(dim_u1, dim_u2)
        s1 = torch.diag(sigma_tsr[indices_u1])
        f2 = FF.repeat(dim_u2).reshape(-1, 1)
        g2 = torch.diag(c.repeat(dim_u2))
        s2 = torch.diag(sigma_tsr[indices_u2])
        invs1os1 = torch.linalg.inv(s1@s1.T)
        s2os2 = s2@s2.T
        mu1 = mu0 + (f2+g2@mu0)*dt + (R0@g1.T) @ invs1os1 @ (du1 -(f1+g1@mu0)*dt)
        R1 = R0 + (g2@R0 + R0@g2.T + s2os2 - R0@g1.T@ invs1os1 @ g1@R0 )*dt
        mu_trace[n] = mu1
        R_trace[n] = R1
        mu0 = mu1
        R0 = R1
    return (mu_trace[cut_point:], R_trace[cut_point:])

def CGFilter_MixModel(mixmodel, u1, mu0, R0, cut_point, sigma_lst):
    # u1, mu0 are in col-matrix form, e.g. (t, x, 1)
    device = u1.device
    sigma_tsr = torch.tensor(sigma_lst)

    indices_u1 = np.array([i for i in range(36) if i % 3 != 2])
    indices_u2 = np.array([i for i in range(36) if i % 3 == 2])
    dim_u1 = len(indices_u1)
    dim_u2 = len(indices_u2)
    dim_u = dim_u1 + dim_u2

    FF = mixmodel.reg[0]
    c = mixmodel.reg[1]

    Nt = u1.shape[0]
    mu_trace = torch.zeros((Nt, dim_u2, 1)).to(device)
    R_trace = torch.zeros((Nt, dim_u2, dim_u2)).to(device)
    mu_trace[0] = mu0
    R_trace[0] = R0
    for n in range(1, Nt):
        du1 = u1[n] - u1[n-1]
        outnet = mixmodel.net(u1[n-1].T) # A tuple with outputs of 3 NNs
        outnet1 = outnet[0].squeeze(0)
        outnet2 = outnet[1].squeeze(0)
        outnet3 = outnet[2].squeeze(0)

        f1 = c*u1[n-1] + FF + torch.stack([outnet1[:,[0]], outnet2[:, [0]]]).permute(1, 0, 2).reshape(-1, 1)
        g1 = torch.zeros(dim_u1, dim_u2)
        g1[torch.arange(24).unsqueeze(dim=1), torch.stack([torch.arange(12)-1, torch.arange(12)]).T.repeat_interleave(2, dim=0)] = \
            torch.stack([outnet1[:,1:], outnet2[:, 1:]]).permute(1, 0, 2).reshape(-1, 2)
        s1 = torch.diag(sigma_tsr[indices_u1])
        f2 = FF + outnet3[:, [0]]
        g2 = torch.diag(c + outnet3[:, 1])
        s2 = torch.diag(sigma_tsr[indices_u2])

        invs1os1 = torch.linalg.inv(s1@s1.T)
        s2os2 = s2@s2.T
        mu1 = mu0 + (f2+g2@mu0)*dt + (R0@g1.T) @ invs1os1 @ (du1 -(f1+g1@mu0)*dt)
        R1 = R0 + (g2@R0 + R0@g2.T + s2os2 - R0@g1.T@ invs1os1 @ g1@R0 )*dt
        mu_trace[n] = mu1
        R_trace[n] = R1
        mu0 = mu1
        R0 = R1
    return (mu_trace[cut_point:], R_trace[cut_point:])


model1 = RegModel()
model2 = MixModel(CGNN())
model3 = MixModel(CGNN())

model1.load_state_dict(torch.load("/home/cc/CodeProjects/CGNSDE/L96/case1/L96(case1)_Model/L96(case1)_regmodel.pt"))
model2.load_state_dict(torch.load("/home/cc/CodeProjects/CGNSDE/L96/case1/L96(case1)_Model/L96(case1)_mixmodel1.pt"))
model3.load_state_dict(torch.load("/home/cc/CodeProjects/CGNSDE/L96/case1/L96(case1)_Model/L96(case1)_mixmodel2_ep1000.pt"))


# sigma estimation
with torch.no_grad():
    train_u_dot_pred1 = model1(None, train_u)
    train_u_dot_pred2 = model2(None, train_u)
sigma_hat1 = torch.sqrt( dt*torch.mean( (train_u_dot - train_u_dot_pred1)**2, dim=0 ) ).tolist()
sigma_hat2 = torch.sqrt( dt*torch.mean( (train_u_dot - train_u_dot_pred2)**2, dim=0 ) ).tolist()

with torch.no_grad():
    mu_preds1, R_preds1 = CGFilter_RegModel(model1, u1=test_u[:, indices_u1].unsqueeze(2), mu0=torch.zeros(dim_u2, 1).to(device), R0=0.01*torch.eye(dim_u2).to(device), cut_point=0, sigma_lst=sigma_hat1)
    mu_preds2, R_preds2 = CGFilter_MixModel(model2, u1=test_u[:, indices_u1].unsqueeze(2), mu0=torch.zeros(dim_u2, 1).to(device), R0=0.01*torch.eye(dim_u2).to(device), cut_point=0, sigma_lst=sigma_hat2)
    mu_preds3, R_preds3 = CGFilter_MixModel(model3, u1=test_u[:, indices_u1].unsqueeze(2), mu0=torch.zeros(dim_u2, 1).to(device), R0=0.01*torch.eye(dim_u2).to(device), cut_point=0, sigma_lst=sigma_hat2)

nnF.mse_loss(test_u[:,indices_u2], mu_preds1.squeeze(2))
nnF.mse_loss(test_u[:,indices_u2], mu_preds2.squeeze(2))
nnF.mse_loss(test_u[:,indices_u2], mu_preds3.squeeze(2))
avg_neg_log_likehood(test_u[:,indices_u2].unsqueeze(2), mu_preds1, R_preds1)
avg_neg_log_likehood(test_u[:,indices_u2].unsqueeze(2), mu_preds2, R_preds2)
avg_neg_log_likehood(test_u[:,indices_u2].unsqueeze(2), mu_preds3, R_preds3)


# Visualizaton
plt.rcParams["text.latex.preamble"] = r"\usepackage{amsmath}"

fig = plt.figure(figsize=(20, 10))
axs = fig.subplots(2, 2)
for ax in fig.get_axes():
    ax.plot(test_t, test_u[:, 2], linewidth=3.5, color="blue")
    ax.set_xlim([100, 150])
    ax.tick_params(labelsize=30, length=8, width=1, direction="in")
    for spine in ax.spines.values():
        spine.set_linewidth(1)
axs[0,0].set_ylabel(r"$x_3$", fontsize=35, rotation=0)
axs[1,0].set_ylabel(r"$x_3$", fontsize=35, rotation=0)
axs[1,0].set_xlabel(r"$t$", fontsize=35)
axs[1,1].set_xlabel(r"$t$", fontsize=35)
axs[0,0].plot(test_t, test_u[:, 2], linewidth=3.5, color="blue", label="True signal")
axs[0,0].plot(test_t, mu_trace[:,0,0],  linewidth=2.5, color="red", label="Posterior mean")
axs[0,1].plot(test_t, mu_preds1[:,0,0], linewidth=2.5, color="red")
axs[1,0].plot(test_t, mu_preds2[:,0,0], linewidth=2.5, color="red")
axs[1,1].plot(test_t, mu_preds3[:,0,0], linewidth=2.5, color="red")
axs[0,0].fill_between(test_t, mu_trace[:, 0, 0]-2*np.sqrt(R_trace[:, 0, 0]), mu_trace[:, 0, 0]+2*np.sqrt(R_trace[:, 0, 0]), color='grey', alpha=0.8, label=r"Uncertainty")
axs[0,1].fill_between(test_t, mu_preds1[:, 0, 0]-2*torch.sqrt(R_preds1[:, 0, 0]), mu_preds1[:, 0, 0]+2*torch.sqrt(R_preds1[:, 0, 0]), color='grey', alpha=0.5)
axs[1,0].fill_between(test_t, mu_preds2[:, 0, 0]-2*torch.sqrt(R_preds2[:, 0, 0]), mu_preds2[:, 0, 0]+2*torch.sqrt(R_preds2[:, 0, 0]), color='grey', alpha=0.8)
axs[1,1].fill_between(test_t, mu_preds3[:, 0, 0]-2*torch.sqrt(R_preds3[:, 0, 0]), mu_preds3[:, 0, 0]+2*torch.sqrt(R_preds3[:, 0, 0]), color='grey', alpha=0.8)
axs[0,0].set_title(r"(a) True model", fontsize=35)
axs[0,1].set_title(r"(b) Physics-based regression model", fontsize=35)
axs[1,0].set_title(r"(c) CGNSDE without DA loss", fontsize=35)
axs[1,1].set_title(r"(d) CGNSDE with DA loss", fontsize=35)
axs[0,0].set_ylim([-11, 13])
axs[0,1].set_ylim([-11, 13])
axs[1,0].set_ylim([-11, 13])
axs[1,1].set_ylim([-11, 13])
axs[0,0].set_yticks([-10, -5, 0, 5, 10])
axs[0,1].set_yticks([-10, -5, 0, 5, 10])
axs[1,0].set_yticks([-10, -5, 0, 5, 10])
axs[1,1].set_yticks([-10, -5, 0, 5, 10])
lege = fig.legend(fontsize=35, loc="upper center", ncol=3, fancybox=False, edgecolor="black", bbox_to_anchor=(0.53, 1))
lege.get_frame().set_linewidth(1)
fig.tight_layout()
fig.subplots_adjust(top=0.8, hspace=0.4)
plt.show()

