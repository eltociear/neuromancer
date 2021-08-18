"""
TODO: eval_metric - evaluate closed loop metric based on the simulation results
# use the same interface for objectives as for the problem via _calculate_loss. if code is changed in problem possible mismatch
TODO: overwrite past after n-steps, continuously in first n steps. In initial simulation period first n-steps are treated as 0s when only first needs to be a 0 vector

"""

import torch
import torch.nn as nn
import numpy as np

from psl import EmulatorBase

from neuromancer.dataset import normalize_01 as normalize, denormalize_01 as min_max_denorm
from neuromancer.problem import Problem
from neuromancer.trainer import move_batch_to_device


class Simulator:
    def __init__(
        self,
        model: Problem,
        train_data,
        dev_data,
        test_data,
        emulator: EmulatorBase = None,
        eval_sim=True,
        device="cpu",
    ):
        self.model = model
        self.train_data = train_data
        self.dev_data = dev_data
        self.test_data = test_data
        self.emulator = emulator
        self.eval_sim = eval_sim
        self.device = device

    def dev_eval(self):
        if self.eval_sim:
            dev_loop_output = self.model(self.dev_data)
        else:
            dev_loop_output = dict()
        return dev_loop_output

    def test_eval(self):
        all_output = dict()
        for data, dname in zip([self.train_data, self.dev_data, self.test_data],
                               ['train', 'dev', 'test']):
            all_output = {
                **all_output,
                **self.simulate(data)
            }
        return all_output

    def simulate(self, data):
        pass


class OpenLoopSimulator(Simulator):
    def __init__(
        self,
        model: Problem,
        train_data,
        dev_data,
        test_data,
        emulator: EmulatorBase = None,
        eval_sim=True,
        device="cpu",
    ):
        super().__init__(
            model=model,
            train_data=train_data,
            dev_data=dev_data,
            test_data=test_data,
            emulator=emulator,
            eval_sim=eval_sim,
            device=device,
        )

    def simulate(self, data):
        return self.model(move_batch_to_device(data, self.device))


class MHOpenLoopSimulator(Simulator):
    """
    moving horizon open loop simulator
    """
    def __init__(self, model: Problem, dataset, emulator: [EmulatorBase, nn.Module] = None,
                 eval_sim=True, device="cpu"):
        super().__init__(model=model, dataset=dataset, emulator=emulator, eval_sim=eval_sim, device=device)

    def horizon_data(self, data, i):
        """
        will work with open loop dataset
        :param data:
        :param i: i-th time step
        :return:
        """
        step_data = {}
        for k, v in data.items():
            step_data[k] = v[i:self.dataset.nsteps+i, :, :]
        step_data["name"] = data["name"]
        return step_data

    def simulate(self, data):
        Y, X, L = [], [], []
        Yp, Yf, Xp, Xf = [], [], [], []
        data = move_batch_to_device(data, self.device)
        yN = data['Yp'][:self.dataset.nsteps, :, :]
        nsim = data['Yp'].shape[0]
        for i in range(nsim-self.nsteps):
            step_data = self.horizon_data(data, i)
            step_data['Yp'] = yN
            step_output = self.model(step_data)
            # outputs
            y_key = [k for k in step_output.keys() if 'Y_pred' in k]
            y = step_output[y_key[0]][0].unsqueeze(0)
            Y.append(y)
            yN = torch.cat([yN, y])[1:]
            yp_key = [k for k in step_output.keys() if 'Yp' in k]
            yp = step_output[yp_key[0]][0].unsqueeze(0)
            Yp.append(yp)
            yf_key = [k for k in step_output.keys() if 'Yf' in k]
            yf = step_output[yf_key[0]][0].unsqueeze(0)
            Yf.append(yf)
            # states
            x_key = [k for k in step_output.keys() if 'X_pred' in k]
            x = step_output[x_key[0]][0].unsqueeze(0)
            X.append(x)
            xp_key = [k for k in step_output.keys() if 'Xp' in k]
            xp = step_output[xp_key[0]][0].unsqueeze(0)
            Xp.append(xp)
            xf_key = [k for k in step_output.keys() if 'Xf' in k]
            xf = step_output[xf_key[0]][0].unsqueeze(0)
            Xf.append(xf)
            loss_keys = [k for k in step_output.keys() if 'loss' in k]
            loss_item = step_output[loss_keys[0]]
            L.append(loss_item)
        output = dict()
        for tensor_list, name in zip([X, Y, L, Yp, Yf, Xp, Xf],
                                     [x_key[0], y_key[0], loss_keys[0],
                                      yp_key[0], yf_key[0],
                                      xp_key[0], xf_key[0]]):
            if tensor_list:
                output[name] = torch.stack(tensor_list)
        return {**data, **output}


class MultiSequenceOpenLoopSimulator(Simulator):
    def __init__(
        self,
        model: Problem,
        train_data,
        dev_data,
        test_data,
        emulator: EmulatorBase = None,
        eval_sim=True,
        stack=False,
        device="cpu",
    ):
        super().__init__(
            model=model,
            train_data=train_data,
            dev_data=dev_data,
            test_data=test_data,
            emulator=emulator,
            eval_sim=eval_sim,
            device=device,
        )
        self.stack = stack

    def agg(self, outputs):
        agg_outputs = dict()
        for k, v in outputs[0].items():
            agg_outputs[k] = []

        for data in outputs:
            for k in data:
                agg_outputs[k].append(data[k])
        for k in agg_outputs:
            if type(agg_outputs[k][0]) == str: continue
            if len(agg_outputs[k][0].shape) < 2:
                agg_outputs[k] = torch.mean(torch.stack(agg_outputs[k]))
            else:
                if self.stack:
                    agg_outputs[k] = torch.stack(agg_outputs[k])
                else:
                    agg_outputs[k] = torch.cat(agg_outputs[k])

        return agg_outputs

    def simulate(self, data):
        outputs = []
        for d in data:
            d = move_batch_to_device(d, self.device)
            outputs.append(self.model(d))
        return self.agg(outputs)

    def dev_eval(self):
        if self.eval_sim:
            dev_loop_output = self.simulate(move_batch_to_device(self.dev_data, self.device))
        else:
            dev_loop_output = dict()
        return dev_loop_output


# TODO: support psl.EmulatorBase as emulator
#  update closed loop simulator with component wrapper
#  around any python emulator mapping input-output keys of the controller model with
#  input-output keys of the emulator
class ClosedLoopSimulator:
    def __init__(
            self,
            sim_data,
            policy: nn.Module,
            emulator: [EmulatorBase, nn.Module],
            estimator: nn.Module = None,
    ):
        """

        :param sim_data:
        :param policy: nn.Module
        :param emulator: nn.Module or psl.EmulatorBase
        :param estimator: nn.Module
        """
        assert isinstance(emulator, EmulatorBase) or isinstance(emulator, nn.Module), \
            f'{type(emulator)} is not EmulatorBase or nn.Module.'
        assert isinstance(policy, nn.Module), \
            f'{type(policy)} is not nn.Module.'
        if estimator is not None:
            assert isinstance(estimator, nn.Module), \
                f'{type(estimator)} is not nn.Module.'
        self.sim_data = sim_data
        self.emulator = emulator
        self.policy = policy
        self.estimator = estimator

    def test_eval(self):
        pass
    # TODO: call simulate and generate output dictionary

    def step_data_policy(self, data, k):
        """
        get one step input data for control policy
        :param data:
        :param k:
        :return:
        """
        step_data = {}
        for key in self.policy.input_keys:
            if key in data.keys():
                step_data[key] = data[key][k - self.policy.nsteps:k, :, :]
                # step_data[key] = step_data[key].reshape(1, step_data[key].shape[0], data[key].shape[2])
        return step_data

    def step_data_estimator(self, data, k):
        """
        get one step input data for state estimator
        :param data:
        :param k:
        :return:
        """
        step_data = {}
        for key in self.estimator.input_keys:
            if key in data.keys():
                # step_data[key] = data[key][k-self.estimator.window_size:k,:,:]
                step_data[key] = data[key][k-self.policy.nsteps:k,:,:]
                # step_data[key] = step_data[key].reshape(1, step_data[key].shape[0], data[key].shape[2])
        return step_data

    def step_data_emulator(self, data, k):
        """
        get one step input data for emulator model
        :param data:
        :param k:
        :return:
        """
        step_data = {}
        for key in self.emulator.input_keys:
            if key in data.keys():
                step_data[key] = data[key][k, :, :]
                # step_data[key] = step_data[key].reshape(1, step_data[key].shape[0], data[key].shape[2])
        return step_data

    def rhc(self, policy_out):
        """
        Receding horizon control = select only first timestep of the control horizon
        :param policy_out:
        :return:
        """
        key = self.policy.output_keys[0]
        # policy_out[key] = policy_out[key][:, [0], :]
        policy_out[key] = policy_out[key][[0], :, :]
        return policy_out

    def append_data(self, sim_data, step_data):
        for key in sim_data.keys():
            sim_data[key].append(step_data[key])
        return sim_data

    def simulate(self, nsim):
        # set initial keys for closed loop simulation data
        cl_keys = self.estimator.output_keys+self.policy.output_keys+self.emulator.output_keys
        cl_keys.remove('reg_error_dynamics')
        cl_keys.remove('reg_error_policy')
        if self.estimator is not None:
            cl_keys.remove('reg_error_estim')
        cl_data = {}
        for key in cl_keys:
            cl_data[key] = []
        # initial time index with offset determined by largest moving horizon window
        start_k = self.policy.nsteps if self.policy.nsteps >= self.estimator.window_size \
            else self.estimator.window_size
        for k in range(start_k, start_k+nsim):
            # estimator step
            if self.estimator is not None:
                step_data = self.step_data_estimator(self.sim_data, k)
                estim_out = self.estimator(step_data)
            else:
                estim_out = {}
            # policy step
            step_data = self.step_data_policy(self.sim_data, k)
            step_data = {**step_data, **estim_out}
            policy_out = self.policy(step_data)     # calculate n-step ahead control
            policy_out = self.rhc(policy_out)       # apply reciding horizon control
            # emulator step
            step_data = self.step_data_emulator(self.sim_data, k)
            step_data = {**step_data, **estim_out, **policy_out}
            emulator_out = self.emulator(step_data)
            # closed-loop step
            cl_step_data = {**estim_out, **policy_out, **emulator_out}
            # append closed-loop step to simulation data
            cl_data = self.append_data(cl_data, cl_step_data)
        # concatenate step data in a single tensor
        for key in cl_data.keys():
            cl_data[key] = torch.cat(cl_data[key])
        return cl_data

