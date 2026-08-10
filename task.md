Your task is to implement a client for an RS485 solar charge controller.

The protocol specification is in the PDF in the manual folder.

**Definition of done:**

The project is coded in python.
The project can be run as a docker container with docker compose.
The project exposes a REST API that returns the telemetry of the controller and offers any command the RS485 allows. The API must perform some sanitation/validation before sending data to the controller.
If the connection is dropped, the project must be able to retry automatically.

When you're done implementing, run the docker container and test the API (read only).

**Guidelines:**

Only use read commands to test the implementation.

Ask questions if things are unclear.

The controller is connected to this computer through an RS232-to-USB adapter. I'm not sure the adapter is compatible with RS485. You'll have to find out the right port.

Use different baud rates if you're not getting good results. I believe the correct baud rate is in the PDF spec.