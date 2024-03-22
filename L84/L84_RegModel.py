import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt
import seaborn as sns
import torch
import torch.nn as nn
import torch.nn.functional as F
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
a, b, f, g = 1/4, 4, 8, 1
sigma_x, sigma_y, sigma_z = 1., 0.05, 0.05

Lt = 250
dt = 0.001
Nt = int(Lt/dt) + 1
t = np.linspace(0, Lt, Nt)
u = np.zeros((Nt, 3))
u[0] = np.ones(3)

for n in range(Nt-1):
    u[n+1, 0] = u[n, 0] + (a * f - a * u[n, 0] - u[n, 1] ** 2 - u[n, 2] ** 2) * dt + sigma_x * np.sqrt(dt) * np.random.randn()
    u[n+1, 1] = u[n, 1] + (g + u[n, 0] * u[n, 1] - u[n, 1] - b * u[n, 0] * u[n, 2]) * dt + sigma_y * np.sqrt(dt) * np.random.randn()
    u[n+1, 2] = u[n, 2] + (b * u[n, 0] * u[n, 1] + u[n, 0] * u[n, 2] - u[n, 2]) * dt + sigma_z * np.sqrt(dt) * np.random.randn()


# Split data in to train and test
u = torch.tensor(u[:-1], dtype=torch.float32)
t = torch.tensor(t[:-1], dtype=torch.float32)

Ntrain = 50000
Ntest = 200000
train_u = u[:Ntrain]
train_t = t[:Ntrain]
test_u = u[-Ntest:]
test_t = t[-Ntest:]


####################################################
################# CGNN & MixModel  #################
####################################################

class RegModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.reg0 = nn.Linear(2, 1)
        self.reg1 = nn.Linear(1, 1)
        self.reg2 = nn.Linear(2, 1)

    def forward(self, t, u):
        basis_x = torch.stack([u[:,0], u[:,2]**2]).T
        basis_y = torch.stack([u[:,1]]).T
        basis_z = torch.stack([u[:,2], u[:,0]*u[:,2]]).T
        x_dyn = self.reg0(basis_x)
        y_dyn = self.reg1(basis_y)
        z_dyn = self.reg2(basis_z)
        self.out = torch.cat([x_dyn, y_dyn, z_dyn], dim=1)
        return self.out

def ODESolver(model, u0, steps, dt):
    # u0 is in vector form, e.g. (x)
    dim = u0.shape[0]
    u_pred = torch.zeros(steps, dim)
    u_pred[0] = u0
    for n in range(0, steps-1):
        u_dot_pred = model(None, u_pred[n].unsqueeze(0)).squeeze(0)
        u_pred[n+1] = u_pred[n]+u_dot_pred*dt
    return u_pred


###################################################
################# Train RegModel  #################
###################################################
short_steps = int(0.2/dt)

# Stage1: Train regmodel with forecast loss
epochs = 10000
train_loss_history = []
train_loss_da_history = []

regmodel = RegModel().to(device)
optimizer = torch.optim.Adam(regmodel.parameters(), lr=1e-3)
for ep in range(1, epochs+1):
    start_time = time.time()
    head_idx_short = torch.from_numpy(np.random.choice(Ntrain-short_steps+1, size=1))
    u_short = train_u[head_idx_short:head_idx_short + short_steps].to(device)
    t_short = train_t[head_idx_short:head_idx_short + short_steps].to(device)

    optimizer.zero_grad()

    out = torchdiffeq.odeint(regmodel, u_short[[0]], t_short)[:,0,:]
    loss = F.mse_loss(u_short, out)

    loss.backward()
    optimizer.step()
    train_loss_history.append(loss.item())
    end_time = time.time()
    print(ep, " loss: ", loss.item(), " time: ", end_time-start_time)

# torch.save(regmodel.state_dict(), r"/home/cc/CodeProjects/CGNN/L84/L84_Model/L84_regmodel.pt")
# np.save(r"/home/cc/CodeProjects/CGNN/L84/L84_Model/L84_regmodel_train_loss.npy", train_loss_history)

# regmodel.load_state_dict(torch.load("/home/cc/CodeProjects/CGNSDE/L84/L84_Model/L84_regmodel.pt"))

##########################################################
################# Estimate sigma  & CGF  #################
##########################################################
train_u_dot = torch.diff(train_u, dim=0)/dt
with torch.no_grad():
    train_u_dot_pred = regmodel(None, train_u[:-1])
sigma_hat = torch.sqrt( dt*torch.mean( (train_u_dot - train_u_dot_pred)**2, dim=0 ) ).tolist()

def CGFilter(regmodel, u1, mu0, R0, cut_point, sigma_lst):
    # u1, mu0 are in col-matrix form, e.g. (t, x, 1)
    device = u1.device
    sigma_x, sigma_y, sigma_z = sigma_lst

    a0 = regmodel.reg0.bias[:]
    a1 = regmodel.reg0.weight[:, 0]
    a2 = regmodel.reg0.weight[:, 1]
    b0 = regmodel.reg1.bias[:]
    b1 = regmodel.reg1.weight[:, 0]
    c0 = regmodel.reg2.bias[:]
    c1 = regmodel.reg2.weight[:, 0]
    c2 = regmodel.reg2.weight[:, 1]

    Nt = u1.shape[0]
    dim_u2 = mu0.shape[0]
    mu_trace = torch.zeros((Nt, dim_u2, 1)).to(device)
    R_trace = torch.zeros((Nt, dim_u2, dim_u2)).to(device)
    mu_trace[0] = mu0
    R_trace[0] = R0
    for n in range(1, Nt):
        y0 = u1[n-1, 0].flatten()
        z0 = u1[n, 1].flatten()
        du1 = u1[n] - u1[n-1]

        f1 = torch.cat([b0+b1*y0, c0+c1*z0]).reshape(-1, 1)
        g1 = torch.cat([torch.zeros(1), c2*z0]).reshape(-1, 1)
        s1 = torch.diag(torch.tensor([sigma_y, sigma_z]))
        f2 = (a0+a2*z0**2).reshape(1,1)
        g2 = (a1).reshape(1,1)
        s2 = torch.tensor([[sigma_x]])

        invs1os1 = torch.linalg.inv(s1@s1.T)
        s2os2 = s2@s2
        mu1 = mu0 + (f2+g2@mu0)*dt + (R0@g1.T) @ invs1os1 @ (du1 -(f1+g1@mu0)*dt)
        R1 = R0 + (g2@R0 + R0@g2.T + s2os2 - R0@g1.T@ invs1os1 @ g1@R0 )*dt
        mu_trace[n] = mu1
        R_trace[n] = R1
        mu0 = mu1
        R0 = R1
    return (mu_trace[cut_point:], R_trace[cut_point:])

def SDESolver(model, u0, steps, dt, sigma_lst):
    # u0 is in vector form, e.g. (x)
    dim = u0.shape[0]
    sigma = torch.tensor(sigma_lst)
    u_simu = torch.zeros(steps, dim)
    u_simu[0] = u0
    for n in range(0, steps-1):
        u_dot_pred = model(None, u_simu[n].unsqueeze(0)).squeeze(0)
        u_simu[n+1] = u_simu[n] + u_dot_pred*dt + sigma*np.sqrt(dt)*torch.randn(3)
    return u_simu

def avg_neg_log_likehood(x, mu, R):
    # x, mu are in matrix form, e.g. (t, x, 1)
    d = x.shape[1]
    neg_log_likehood = 1/2*(d*np.log(2*np.pi) + torch.log(torch.linalg.det(R)) + ((x-mu).permute(0,2,1)@torch.linalg.inv(R)@(x-mu)).flatten())
    return torch.mean(neg_log_likehood)

#################################################
################# Test RegModel #################
#################################################

# Short-term Prediction
def integrate_batch(t, u, model, batch_steps):
    # u is in vector form, e.g. (t, x)
    device = u.device
    Nt = u.shape[0]
    num_batchs = int(Nt / batch_steps)
    error_abs = 0
    # error_rel = 0
    u_pred = torch.tensor([]).to(device)
    for i in range(num_batchs):
        u_batch = u[i*batch_steps: (i+1)*batch_steps]
        with torch.no_grad():
            u_batch_pred = torchdiffeq.odeint(model, u_batch[[0]], t[:batch_steps])[:,0,:]
        u_pred = torch.cat([u_pred, u_batch_pred])
        error_abs += torch.mean( (u_batch - u_batch_pred)**2 ).item()
        # error_rel += torch.mean( torch.norm(stt_batch - stt_pred_batch, 2, 1) / (torch.norm(stt_batch, 2, 1)) ).item()
    error_abs /= num_batchs
    # error_rel /= num_batch
    return [u_pred, error_abs]
u_shortPreds, error_abs = integrate_batch(test_t, test_u, regmodel, short_steps)

fig = plt.figure(figsize=(12, 10))
axs = fig.subplots(3, 1, sharex=True)
axs[0].plot(test_t, test_u[:, 0], linewidth=3)
axs[0].plot(test_t, u_shortPreds[:, 0],linestyle="dashed", linewidth=2)
axs[0].set_ylabel(r"$x$", fontsize=25, rotation=0)
axs[0].set_title(r"\textbf{Short-term Prediction by MixModel-DA}", fontsize=30)
axs[1].plot(test_t, test_u[:, 1], linewidth=3)
axs[1].plot(test_t, u_shortPreds[:, 1],linestyle="dashed", linewidth=2)
axs[1].set_ylabel(r"$y$", fontsize=25, rotation=0)
axs[2].plot(test_t, test_u[:, 2], linewidth=3)
axs[2].plot(test_t, u_shortPreds[:, 2],linestyle="dashed", linewidth=2)
axs[2].set_ylabel(r"$z$", fontsize=25, rotation=0)
axs[2].set_xlabel(r"$t$", fontsize=25)
for ax in fig.get_axes():
    ax.tick_params(labelsize=25, length=7, width=2)
    for spine in ax.spines.values():
        spine.set_linewidth(2)
fig.tight_layout()
plt.show()


# Data Assimilation
with torch.no_grad():
    mu_preds, R_preds = CGFilter(regmodel, u1=test_u[:, 1:].reshape(-1, 2, 1), mu0=torch.zeros(1, 1).to(device), R0=0.01*torch.eye(1).to(device), cut_point=0, sigma_lst=sigma_hat)
F.mse_loss(test_u[:,[0]], mu_preds.reshape(-1, 1))
avg_neg_log_likehood(test_u[:,[0]].unsqueeze(2), mu_preds, R_preds)

fig = plt.figure(figsize=(10, 4))
ax = fig.subplots(1, 1)
ax.plot(test_t, test_u[:, 0], linewidth=3, label=r"\textbf{True System}")
ax.plot(test_t, mu_preds[:, 0, 0], linewidth=1.5, linestyle="dashed", label=r"\textbf{DA Mean}")
ax.fill_between(test_t, mu_preds[:, 0, 0]-2*torch.sqrt(R_preds[:, 0, 0]), mu_preds[:, 0, 0]+2*torch.sqrt(R_preds[:, 0, 0]), color='C1', alpha=0.2, label=r"\textbf{Uncertainty}")
ax.set_ylabel(r"$x$", fontsize=30, rotation=0)
ax.set_title(r"\textbf{RegModel}", fontsize=30)
ax.tick_params(labelsize=30)
for ax in fig.get_axes():
    ax.tick_params(labelsize=25, length=7, width=2)
    for spine in ax.spines.values():
        spine.set_linewidth(2)
fig.tight_layout()
plt.show()


# Long-term Simulation
torch.manual_seed(0)
np.random.seed(0)
with torch.no_grad():
    u_longSimu = SDESolver(regmodel, test_u[0], steps=Ntest, dt=0.001, sigma_lst=sigma_hat)

test_u = test_u.numpy()
u_longSimu = u_longSimu.numpy()

def acf(x, lag=2000):
    i = np.arange(0, lag+1)
    v = np.array([1]+[np.corrcoef(x[:-i], x[i:])[0,1]  for i in range(1, lag+1)])
    return (i, v)
t_lags = np.linspace(0, 2, 2001)


plt.rcParams["text.latex.preamble"] = r"\usepackage{amsmath}"
fig = plt.figure(figsize=(22, 12))
# x dynamics
ax00 = plt.subplot2grid((3, 5), (0, 0), colspan=3)
ax00.plot(test_t, test_u[:, 0], linewidth=2, label="True signal", color="blue")
ax00.plot(test_t, u_longSimu[:, 0], linewidth=2, label="Physics-based regression model", color="red")
ax00.set_ylim( [min(np.min(test_u[:,0]),np.min(u_longSimu[:,0])), max(np.max(test_u[:,0]), np.max(u_longSimu[:,0]))] )
ax00.set_ylabel(r"$x$", fontsize=35, rotation=0, labelpad=25)
ax00.set_title(r"{(a) True signal", fontsize=35)
ax01 = plt.subplot2grid((3, 5), (0, 3))
sns.kdeplot(test_u[:, 0], ax=ax01, linewidth=3, bw_adjust=2, color="blue")
sns.kdeplot(u_longSimu[:, 0], ax=ax01, linewidth=3, bw_adjust=2, color="red")
ax01.set_ylabel("")
ax01.set_xlim( [min(np.min(test_u[:,0]),np.min(u_longSimu[:,0])), max(np.max(test_u[:,0]), np.max(u_longSimu[:,0]))] )
ax01.set_title(r"(b) PDF", fontsize=35)
ax02 = plt.subplot2grid((3, 5), (0, 4))
ax02.plot(t_lags, acf(test_u[:, 0])[1], linewidth=3, color="blue")
ax02.plot(t_lags, acf(u_longSimu[:, 0])[1], linewidth=3, color="red")
ax02.set_title(r"(c) ACF", fontsize=35)
ax02.set_yticks(np.arange(0, 1+0.5, 0.5))
ax02.set_xticks(np.linspace(0, 2, 3))
# y dynamics
ax10 = plt.subplot2grid((3, 5), (1, 0), colspan=3)
ax10.plot(test_t, test_u[:, 1], linewidth=2, color="blue")
ax10.plot(test_t, u_longSimu[:, 1], linewidth=2, color="red")
ax10.set_ylim( [min(np.min(test_u[:,1]),np.min(u_longSimu[:,1])), max(np.max(test_u[:,1]), np.max(u_longSimu[:,1]))] )
ax10.set_ylabel(r"$y$", fontsize=35, rotation=0, labelpad=25)
ax11 = plt.subplot2grid((3, 5), (1, 3))
sns.kdeplot(test_u[:, 1], ax=ax11, linewidth=3, bw_adjust=2, color="blue")
sns.kdeplot(u_longSimu[:, 1], ax=ax11, linewidth=3, bw_adjust=2, color="red")
ax11.set_xlim( [min(np.min(test_u[:,1]),np.min(u_longSimu[:,1])), max(np.max(test_u[:,1]), np.max(u_longSimu[:,1]))] )
ax11.set_ylabel("")
ax12 = plt.subplot2grid((3, 5), (1, 4))
ax12.plot(t_lags, acf(test_u[:, 1])[1], linewidth=3, color="blue")
ax12.plot(t_lags, acf(u_longSimu[:, 1])[1], linewidth=3, color="red")
ax12.set_yticks(np.arange(0, 1+0.5, 0.5))
ax12.set_xticks(np.linspace(0, 2, 3))
# z dynamics
ax20 = plt.subplot2grid((3, 5), (2, 0), colspan=3)
ax20.plot(test_t, test_u[:, 2], linewidth=2, color="blue")
ax20.plot(test_t, u_longSimu[:, 2], linewidth=2, color="red")
ax20.set_ylim( [min(np.min(test_u[:,2]),np.min(u_longSimu[:,2])), max(np.max(test_u[:,2]), np.max(u_longSimu[:,2]))] )
ax20.set_ylabel(r"$z$", fontsize=35, rotation=0, labelpad=8)
ax20.set_xlabel(r"$t$", fontsize=35)
ax21 = plt.subplot2grid((3, 5), (2, 3))
sns.kdeplot(test_u[:, 2], ax=ax21, linewidth=3, bw_adjust=2, color="blue")
sns.kdeplot(u_longSimu[:, 2], ax=ax21, linewidth=3, bw_adjust=2, color="red")
ax21.set_xlim( [min(np.min(test_u[:,2]),np.min(u_longSimu[:,2])), max(np.max(test_u[:,2]), np.max(u_longSimu[:,2]))] )
ax21.set_ylabel("")
ax22 = plt.subplot2grid((3, 5), (2, 4))
ax22.plot(t_lags, acf(test_u[:, 2])[1], linewidth=3, color="blue")
ax22.plot(t_lags, acf(u_longSimu[:, 2])[1], linewidth=3, color="red")
ax22.set_xlabel(r"$t$", fontsize=35)
ax22.set_yticks(np.arange(0, 1+0.5, 0.5))
ax22.set_xticks(np.linspace(0, 2, 3))
for ax in fig.get_axes():
    ax.tick_params(labelsize=30, length=8, width=1, direction="in")
    for spine in ax.spines.values():
        spine.set_linewidth(1)
ax00.set_xlim([50, 250])
ax10.set_xlim([50, 250])
ax20.set_xlim([50, 250])
ax01.set_ylim([0, 0.65])
ax11.set_ylim([0, 0.65])
ax21.set_ylim([0, 0.65])
ax00.set_yticks([-2, 0, 2])
ax10.set_yticks([-4, -2, 0, 2])
ax20.set_yticks([-2.5, 0, 2.5])
ax01.set_xticks([-2, 0, 2])
ax11.set_xticks([-4, -2, 0, 2])
ax21.set_xticks([-2.5, 0, 2.5])
ax02.set_xlim([0, 2])
ax12.set_xlim([0, 2])
ax22.set_xlim([0, 2])
lege = fig.legend(fontsize=35, loc="upper center", ncol=2, fancybox=False, edgecolor="black", bbox_to_anchor=(0.53, 1))
lege.get_frame().set_linewidth(1)
fig.tight_layout()
fig.subplots_adjust(top=0.85)
# Last alignment (Not necesary)
ax00.set_ylim([-4.5, 4.5])
ax10.set_ylim([-4.5, 4.5])
ax20.set_ylim([-4.5, 4.5])
ax01.set_xlim([-4, 4])
ax11.set_xlim([-4, 4])
ax21.set_xlim([-4, 4])
ax00.set_yticks([-4,-2, 0, 2, 4])
ax10.set_yticks([-4,-2, 0, 2, 4])
ax20.set_yticks([-4,-2, 0, 2, 4])
ax01.set_xticks([-4,-2, 0, 2, 4])
ax11.set_xticks([-4,-2, 0, 2, 4])
ax21.set_xticks([-4,-2, 0, 2, 4])
plt.show()

