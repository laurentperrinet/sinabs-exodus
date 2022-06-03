from typing import Callable, Optional, Union

import torch
from sinabs.layers import SqueezeMixin
from sinabs.layers import LIF as LIFSinabs
from sinabs.activation import (
    MultiSpike,
    MaxSpike,
    MembraneSubtract,
    SingleExponential,
)

from sinabs.exodus.leaky import LeakyIntegrator
from sinabs.exodus.spike import IntegrateAndFire

__all__ = ["LIF", "LIFSqueeze"]


class LIF(LIFSinabs):
    """
    Exodus implementation of a Leaky Integrate and Fire neuron layer.

    Neuron dynamics in discrete time:

    .. math ::
        V_{mem}(t+1) = \\alpha V_{mem}(t) + (1-\\alpha)\\sum z(t)

        \\text{if } V_{mem}(t) >= V_{th} \\text{, then } V_{mem} \\rightarrow V_{reset}

    where :math:`\\alpha =  e^{-1/tau_{mem}}` and :math:`\\sum z(t)` represents the sum of all input currents at time :math:`t`.

    Parameters
    ----------
    tau_mem: float
        Membrane potential time constant.
    tau_syn: float
        Synaptic decay time constants. If None, no synaptic dynamics are used, which is the default.
    spike_threshold: float
        Spikes are emitted if v_mem is above that threshold. By default set to 1.0.
    spike_fn: torch.autograd.Function
        Choose a Sinabs or custom torch.autograd.Function that takes a dict of states,
        a spike threshold and a surrogate gradient function and returns spikes. Be aware
        that the class itself is passed here (because torch.autograd methods are static)
        rather than an object instance.
    reset_fn: Callable
        A function that defines how the membrane potential is reset after a spike.
    surrogate_grad_fn: Callable
        Choose how to define gradients for the spiking non-linearity during the
        backward pass. This is a function of membrane potential.
    min_v_mem: float or None
        Lower bound for membrane potential v_mem, clipped at every time step.
    train_alphas: bool
        When True, the discrete decay factor exp(-1/tau) is used for training rather than tau itself.
    shape: torch.Size
        Optionally initialise the layer state with given shape. If None, will be inferred from input_size.
    norm_input: bool
        When True, normalise input current by tau. This helps when training time constants.
    record_states: bool
        When True, will record all internal states such as v_mem or i_syn in a dictionary attribute `recordings`. Default is False.
    """
    def __init__(
        self,
        tau_mem: Union[float, torch.Tensor],
        tau_syn: Optional[Union[float, torch.Tensor]] = None,
        spike_threshold: float = 1.0,
        spike_fn: Callable = MultiSpike,
        reset_fn: Callable = MembraneSubtract(),
        surrogate_grad_fn: Callable = SingleExponential(),
        min_v_mem: Optional[float] = None,
        train_alphas: bool = False,
        shape: Optional[torch.Size] = None,
        norm_input: bool = True,
        record_states: bool = False,
    ):
        # Make sure activation functions match exodus specifications
        self._parse_activation_fn(spike_fn, reset_fn)

        super().__init__(
            tau_mem=tau_mem,
            tau_syn=tau_syn,
            spike_threshold=spike_threshold,
            spike_fn=spike_fn,
            reset_fn=reset_fn,
            surrogate_grad_fn=surrogate_grad_fn,
            min_v_mem=min_v_mem,
            train_alphas=train_alphas,
            shape=shape,
            norm_input=norm_input,
            record_states=record_states,
        )

        # Add activations as buffer
        self.register_buffer("activation", torch.zeros((0)))


    def _parse_activation_fn(self, spike_fn, reset_fn):

        if spike_fn is None:
            # Non-spiking neurons
            return

        if (
            spike_fn not in (MultiSpike, SingleSpike)
            and not isinstance(spike_fn, MaxSpike)
        ) or not isinstance(reset_fn, MembraneSubtract):
            raise NotImplementedError(
                "Spike mechanism config not supported. "
                "Use MultiSpike/SingleSpike/MaxSpike and MembraneSubtract functions."
            )

        if isinstance(spike_fn, MaxSpike):
            self.max_num_spikes_per_bin = spike_fn.max_num_spikes_per_bin
        elif spike_fn == MultiSpike:
            self.max_num_spikes_per_bin = None
        else:
            self.max_num_spikes_per_bin = 1

    def _prepare_input(self, input_data: torch.Tensor):
        batch_size, time_steps, *trailing_dim = input_data.shape

        # Ensure the neuron state are initialized
        if not self.is_state_initialised() or not self.state_has_shape(
            (batch_size, *trailing_dim)
        ):
            self.init_state_with_shape((batch_size, *trailing_dim))

        # Move time to last dimension -> (n_batches, *trailing_dim, num_timesteps)
        # Flatten out all dimensions that can be processed in parallel and ensure contiguity
        input_2d = input_data.movedim(1, -1).reshape(-1, num_timesteps)

        return input_2d, batch_size, time_steps, *trailing_dim


    def _forward_synaptic(self, input_2d: torch.Tensor):
        """ Evolve synaptic dynamics """

        # Apply exponential filter to input
        return LeakyIntegrator.apply(
            input_2d,  # Input data
            self.i_syn.flatten().contiguous(),  # Initial synaptic states
            self.alpha_syn_calculated.expand(input_2d[:,0].shape)  # Synaptic alpha
            self.train_alphas,  # Should grad for alphas be calculated
        )

    def _forward_membrane(self, i_syn_2d: torch.Tensor):
        """ Evolve membrane dynamics """
            
        # Broadcast alpha to number of neurons (x batches)
        alpha_mem = self.alpha_mem_calculated.expand(i_syn_2d[:,0].shape)

        if self.norm_input:
            # Rescale input with 1 - alpha
            i_syn_2d = (1.0 - alpha_mem) * i_syn_2d

        if self.spike_fn is None:
            # - Non-spiking case (leaky integrator)
            v_mem = LeakyIntegrator.apply(
                i_syn_2d,  # Input data
                self.v_mem.flatten().contiguous(),  # Initial vmem
                alpha_mem  # Membrane alpha
                self.train_alphas,  # Should grad for alphas be calculated
            )

            return v_mem, v_mem


        return IntegrateAndFire.apply(
            i_syn_2d.contiguous(),  # Input data
            alpha_mem,  # Alphas
            self.v_mem.flatten().contiguous(),  # Initial vmem
            self.activations.flatten(),  # Initial activations
            self.spike_threshold,  # Spike threshold
            self.spike_threshold,  # Membrane subtract
            self.min_v_mem,  # Lower bound on vmem
            self.surrogate_grad_fn,  # Surrogate gradient
            self.max_num_spikes_per_bin,  # Max. number of spikes per bin
            self.train_alphas,  # Should grad for alphas be calculated
        )


    def forward(self, input_data: torch.Tensor):
        """
        Forward pass with given data.

        Parameters:
            input_current : torch.Tensor
                Data to be processed. Expected shape: (batch, time, ...)

        Returns:
            torch.Tensor
                Output data. Same shape as `input_data`.
        """

        input_2d, batch_size, time_steps, *trailing_dim = self._prepare_input(input_data)
        
        self.recordings = dict()

        # - Synaptic dynamics
        if self.tau_syn is None:
            i_syn_2d = input_2d
        else:
            i_syn_2d = self._forward_synaptic(input_2d)

            # Bring i_syn to shape that matches input
            i_syn_full = i_syn_2d.reshape(n_batches, *trailing_dim, -1).movedim(-1, 1)
            
            # Update internal i_syn
            self.i_syn = i_syn_full[:, -1].clone()
            if self.record_states:
                self.recordings["i_syn"] = i_syn_full

        # - Membrane dynamics
        output_2d, v_mem_2d = self._forward_membrane(i_syn_2d)

        # Reshape output spikes and v_mem_full, store neuron states
        v_mem_full = v_mem_2d.reshape(n_batches, *n_neurons, -1).movedim(-1, 1)
        output_full = output_2d.reshape(n_batches, *n_neurons, -1).movedim(-1, 1)

        if self.record_states:
            recordings["v_mem"] = v_mem_full

        # update neuron states
        self.v_mem = v_mem_full[:, -1].clone()

        self.firing_rate = output_full.sum() / output_full.numel()

        return output_full


class LIFSqueeze(LIF, SqueezeMixin):
    """
    Same as parent class, only takes in squeezed 4D input (Batch*Time, Channel, Height, Width)
    instead of 5D input (Batch, Time, Channel, Height, Width) in order to be compatible with
    layers that can only take a 4D input, such as convolutional and pooling layers.
    """

    def __init__(self, batch_size=None, num_timesteps=None, **kwargs):
        super().__init__(**kwargs)
        self.squeeze_init(batch_size, num_timesteps)

    def forward(self, input_data: torch.Tensor) -> torch.Tensor:
        return self.squeeze_forward(input_data, super().forward)

    @property
    def _param_dict(self) -> dict:
        return self.squeeze_param_dict(super()._param_dict)
