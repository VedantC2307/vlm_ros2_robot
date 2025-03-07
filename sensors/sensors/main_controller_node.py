import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from robot_messages.action import LLMTrigger, MotorControl
import json
import asyncio
import websockets
import ssl
from threading import Thread, Event
from geometry_msgs.msg import Vector3
import os

class MainController(Node):
    def __init__(self):

        super().__init__('main_controller')
        self.get_logger().info("Main Controller Node Started")

        # Action Clients
        self.llm_action_client = ActionClient(self, LLMTrigger, 'llm_interaction')
        self.motor_control_client = ActionClient(self, MotorControl, 'motor_control')

        # Use the IP address from the environment or default
        ip_address = os.getenv("IP_ADDRESS")

        websocket_prompt_uri = f"wss://{ip_address}:4000/transcription"
        self.declare_parameter('websocket_prompt_uri', websocket_prompt_uri)
        self.websocket_prompt_uri = self.get_parameter('websocket_prompt_uri').get_parameter_value().string_value

        # Set up SSL context
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

        # Create an asyncio event loop for WebSocket communication
        self.loop = asyncio.new_event_loop()
        self.shutdown_event = Event()

        # Start WebSocket listener thread
        self.websocket_thread = Thread(target=self.run_async_loop, daemon=True)
        self.websocket_thread.start()

        self.current_prompt = None
        self.processing_prompt = False
        self.last_processed_prompt = None   

        # Subscriber for pose data
        self.subscription = self.create_subscription(
            Vector3,
            '/robot_position',
            self.position_callback,
            2
        )
        
        self.current_position = None
        self.start_position = None
        # self.main_logic_ready = True
    
        # Create the timer without making it async
        self.create_timer(5.0, self.timer_callback)

    # def timer_callback(self):
    #     """Non-async timer callback that creates and runs the coroutine"""
    #     if self.main_logic_ready:
    #         Thread(target=self.main_logic).start()

    def timer_callback(self):
        """Non-async timer callback that creates and runs the coroutine"""
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.main_logic(), self.loop)

    def run_async_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.listen_to_websocket())
        except asyncio.CancelledError:
            self.get_logger().info("WebSocket listener stopped.")
        finally:
            self.loop.close()

    async def listen_to_websocket(self):
        """
        Connect to the WebSocket server and continuously listen for prompt updates.
        """
        while not self.shutdown_event.is_set():
            try:
                self.get_logger().info(f"Connecting to WebSocket server at {self.websocket_prompt_uri}...")
                async with websockets.connect(self.websocket_prompt_uri, ssl=self.ssl_context) as websocket:
                    self.get_logger().info("Connected to WebSocket server.")
                    async for message in websocket:
                        if self.shutdown_event.is_set():
                            break
                        try:
                            self.get_logger().debug(f"Raw message received: {message}")
                            data = json.loads(message)
                            if 'message' in data and 'prompt' in data['message']:
                                self.current_prompt = data['message']['prompt']
                                self.get_logger().info(f"Received new prompt: {self.current_prompt}")
                            else:
                                self.get_logger().warning("Received message without prompt data.")
                        except json.JSONDecodeError:
                            self.get_logger().error(f"Invalid JSON received: {message}")
                        except Exception as e:
                            self.get_logger().error(f"Error processing prompt data: {str(e)}")
            except websockets.exceptions.ConnectionClosed as e:
                self.get_logger().warning(f"WebSocket connection closed: {str(e)}. Retrying in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                self.get_logger().error(f"WebSocket error: {str(e)}. Retrying in 5 seconds...")
                await asyncio.sleep(5)

    def position_callback(self, msg):
        """Callback function for processing the /robot_position topic."""
        self.current_position = msg
        if self.start_position is None: # if the start position is none, then it has not been initialized.
            self.start_position = msg
        self.get_logger().debug(f"Current robot position: x={msg.x}, z={msg.z}, roll={msg.y}")

    async def main_logic(self):
        """
        Main logic of the controller:
        1. Send prompt to LLM action server.
        2. Process LLM result.
        3. Send command to motor control action server.
        """
        try:
            # self.main_logic_ready = False
            # Check if we have a valid prompt
            if self.processing_prompt or not self.current_prompt:
                # self.get_logger().debug("No prompt available, skipping main logic loop")
                return
            
            if self.current_prompt == self.last_processed_prompt:
                return
            
            self.processing_prompt = True
            self.last_processed_prompt = self.current_prompt
            task_complete = False
            start_x = round(self.start_position.x, 2)
            start_y = round(self.start_position.y,2)
            start_z = round(self.start_position.z, 2)

            robot_position = f"The robots starting position is x:{start_z}, y:{start_x}, z:{start_y}. Your current position is x:{self.current_position.z}, y:{self.current_position.x}, z:{self.current_position.y}"
            prompt = self.current_prompt + ". " + robot_position
            
            # while not task_complete:
            # 1. Send prompt to LLM action server
            llm_goal_handle = await self.send_goal_to_llm_server(prompt)
            if not llm_goal_handle:
                self.get_logger().error("LLM goal was rejected")
                self.processing_prompt = False
                return

            llm_result = await self.get_result_from_llm_server(llm_goal_handle)
            if not llm_result:
                self.get_logger().error("Failed to get result from LLM server")
                self.processing_prompt = False
                return
            
            self.get_logger().info(f"LLM Response: {llm_result}")

            # 2. Process LLM result
            motor_command, distance, task_complete, previous_task = self.process_llm_result(llm_result)
            if not motor_command:
                self.get_logger().error("Could not determine motor command from LLM result")
                self.processing_prompt = False
                return

            # 3. Send command to motor control action server
            motor_goal_handle = await self.send_goal_to_motor_server(motor_command, distance)
            if not motor_goal_handle:
                self.get_logger().error("Motor control goal was rejected")
                self.processing_prompt = False
                return

            motor_result = await self.get_result_from_motor_server(motor_goal_handle)
            if not motor_result:
                self.get_logger().error("Failed to get result from motor control server")
            elif motor_result.success:
                self.get_logger().info("Motor control action succeeded")
            else:
                self.get_logger().info("Motor control action failed")

            if not task_complete:
                prompt = f"{prompt}. Status: Task Incomplete. Previously performed tasked JSON Object:{json.dumps(previous_task)}"

        except Exception as e:
            self.get_logger().error(f"Error in main logic: {str(e)}")
        finally:
            self.processing_prompt = False
            # self.main_logic_ready = True

    async def send_goal_to_llm_server(self, prompt):
        """Sends the prompt to the LLM action server."""
        try:
            # Ensure prompt is a string and not None
            if not isinstance(prompt, str) or not prompt.strip():
                self.get_logger().error("Invalid prompt received. Prompt must be a non-empty string.")
                return None

            goal_msg = LLMTrigger.Goal()
            goal_msg.prompt = prompt
            self.get_logger().info(f"Sending prompt to LLM action server: {prompt}")

            # Wait for server without await
            if not self.llm_action_client.wait_for_server(timeout_sec=10.0):
                self.get_logger().error("LLM action server not available within timeout.")
                return None
            self.get_logger().info("LLM action server available.")

            # Send goal asynchronously
            send_goal_future = await self.llm_action_client.send_goal_async(goal_msg)
            
            if not send_goal_future.accepted:
                self.get_logger().warning('LLM goal rejected')
                return None

            self.get_logger().info('LLM goal accepted')
            return send_goal_future

        except Exception as e:
            self.get_logger().error(f"Error sending goal to LLM server: {str(e)}")
            return None

    async def get_result_from_llm_server(self, goal_handle):
        """Gets the result from the LLM action server."""
        try:
            result_future = await goal_handle.get_result_async()
            
            if not result_future:
                self.get_logger().error('LLM action failed')
                return None

            return result_future.result

        except Exception as e:
            self.get_logger().error(f"Error getting result from LLM server: {str(e)}")
            return None

    async def send_goal_to_motor_server(self, command, distance):
        """Sends the command and distance to the motor control action server."""
        try:
            goal_msg = MotorControl.Goal()
            goal_msg.command = command
            goal_msg.distance = float(distance)

            if not self.motor_control_client.wait_for_server(timeout_sec=10.0):
                self.get_logger().error("Motor action server not available within timeout.")
                return None
            self.get_logger().info("Motor action server available.")

            send_goal_future = await self.motor_control_client.send_goal_async(goal_msg)
            
            if not send_goal_future.accepted:
                self.get_logger().warn('Motor control goal rejected')
                return None

            self.get_logger().info('Motor control goal accepted')
            return send_goal_future

        except Exception as e:
            self.get_logger().error(f"Error sending goal to motor server: {str(e)}")
            return None

    async def get_result_from_motor_server(self, goal_handle):
        """Gets the result from the motor control action server."""
        try:
            result_future = await goal_handle.get_result_async()
            
            if not result_future:
                self.get_logger().error('Motor control action failed')
                return None

            return result_future.result  # Remove the () since it's not callable

        except Exception as e:
            self.get_logger().error(f"Error getting result from motor server: {str(e)}")
            return None

        except Exception as e:
            self.get_logger().error(f"Error getting result from motor server: {str(e)}")
            return None

    def process_llm_result(self, llm_result):
        """
        Processes the JSON result from the LLM server and extracts the motor command and distance.
        """
        try:
            if not llm_result.llm_response:
                self.get_logger().error("Empty response from LLM server")
                return None, None

            data = json.loads(llm_result.llm_response)
            if not data:
                self.get_logger().error("Empty JSON data from LLM response")
                return None, None

            if "command" not in data:
                self.get_logger().error("No command found in LLM response")
                return None, None
            
            task_complete = data.get("task_complete", False)
            
            # data = json.loads(llm_result.llm_response)
            if "MOVE_FORWARD" in data["command"]:
                motor_command = "MOVE_FORWARD"
                distance = data.get("linear_distance", 0.0)  # Default distance of 0.0 meters
                self.get_logger().info(f"Extracted command: {motor_command}, distance: {distance}")
                return motor_command, distance, task_complete, data
            
            if "MOVE_BACKWARD" in data["command"]:
                motor_command = "MOVE_BACKWARD"
                distance = data.get("linear_distance", 0.0)  # Default distance of 0.0 meters
                self.get_logger().info(f"Extracted command: {motor_command}, distance: {distance}")
                return motor_command, distance, task_complete, data
            
            elif "ROTATE_CLOCKWISE" in data["command"]:
                motor_command = "ROTATE_CLOCKWISE"
                distance = data.get("rotate_degree", 0.0)  # Default rotation of 0.0 meters
                self.get_logger().info(f"Extracted command: {motor_command}, distance: {distance}")
                return motor_command, distance, task_complete, data
            
            elif "ROTATE_COUNTERCLOCKWISE" in data["command"]:
                motor_command = "ROTATE_COUNTERCLOCKWISE"
                distance = data.get("rotate_degree", 0.0)  # Default rotation of 0.0 meters
                self.get_logger().info(f"Extracted command: {motor_command}, distance: {distance}")
                return motor_command, distance, task_complete, data
            
            elif "WAIT" in data["command"]:
                motor_command = "WAIT"
                self.get_logger().info(f"Extracted command: {motor_command}")
                return motor_command, distance, task_complete, data
            else:
                self.get_logger().warn(f"Unknown action from LLM: {data['command']}")
                return None, None
        except (json.JSONDecodeError, KeyError) as e:
            self.get_logger().error(f"Error processing LLM result: {e}")
            return None, None

def main(args=None):
    rclpy.init(args=args)
    main_controller = MainController()
    try:
        rclpy.spin(main_controller)
    except KeyboardInterrupt:
        main_controller.get_logger().info("Keyboard interrupt, shutting down")
    finally:
        main_controller.shutdown_event.set()
        main_controller.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()