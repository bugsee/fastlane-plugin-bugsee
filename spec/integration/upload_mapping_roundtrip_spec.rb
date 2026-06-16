# frozen_string_literal: true

# REAL round-trip integration test for upload_mapping_to_bugsee.
#
# Unlike the unit spec (which stubs Actions.sh and asserts on the
# constructed command string), this test drives the FULL chain:
#
#   described_class.run
#     -> Actions.sh
#       -> BugseeAgent (Python)
#         -> bugsee-cli (real native binary, Rust/reqwest)
#           -> HTTP to a REAL local TCPServer mock
#
# Because the CLI makes its own HTTP calls (reqwest, not Ruby
# Net::HTTP), WebMock cannot intercept them — we MUST stand up a real
# socket server. We use Ruby stdlib Socket/TCPServer in a background
# thread (zero new gems) and speak the two-stage presigned-upload
# protocol the CLI expects:
#
#   Stage 1: POST {endpoint}/apps/{app_token}/symbols
#            (JSON metadata body, header X-Bugsee-Uploader: cli)
#            -> 200 {"code":0,"endpoint":"http://127.0.0.1:<port>/put/x"}
#   Stage 2: PUT <that url> with the zip body
#            -> 200
#
# We don't validate or unzip the body — we only record that BOTH legs
# were hit. The CLI opens a separate connection per leg, so the mock
# loops accepting connections until the test tears it down.
#
# This spec is tagged :integration and is EXCLUDED from the default
# `bundle exec rspec` run (see spec_helper.rb). It additionally skips
# itself unless BUGSEE_CLI_PATH points at an executable binary, so a
# developer who opts in via RUN_INTEGRATION but has no CLI locally
# still gets a clean (pending) run rather than a failure.

require 'spec_helper'
require 'socket'
require 'json'
require 'tempfile'
require 'thread'

describe Fastlane::Actions::UploadMappingToBugseeAction, :integration do
  # ----------------------------------------------------------------
  # A tiny single-threaded HTTP/1.1 server that speaks just enough of
  # the protocol for the two-stage upload. Records each [method, path]
  # onto a thread-safe Queue. Lives in a background thread; the test
  # body kills it in an ensure block.
  # ----------------------------------------------------------------
  class MockSymbolServer
    attr_reader :port, :hits

    def initialize
      # Port 0 => OS assigns an ephemeral free port. Bind to loopback
      # only so we never expose the mock beyond the test host.
      @server = TCPServer.new('127.0.0.1', 0)
      @port   = @server.addr[1]
      @hits   = Queue.new
      @thread = nil
    end

    def start
      @thread = Thread.new { accept_loop }
      self
    end

    # Drain the recorded hits into a plain Array for assertions.
    def recorded_hits
      out = []
      out << @hits.pop until @hits.empty?
      out
    end

    def stop
      # Closing the listening socket makes a blocked `accept` raise,
      # which breaks the loop. Then join with a short timeout and
      # hard-kill as a backstop so a wedged thread can't hang the suite.
      begin
        @server.close
      rescue StandardError
        # already closed
      end
      if @thread
        @thread.join(2)
        @thread.kill if @thread.alive?
      end
    end

    private

    def accept_loop
      loop do
        client =
          begin
            @server.accept
          rescue StandardError
            break # server socket closed during teardown
          end
        handle_client(client)
      end
    end

    def handle_client(client)
      method, path, headers = read_request_head(client)
      return if method.nil?

      # Consume the request body (if any) so the client's write side
      # doesn't block on a full socket buffer before we reply.
      if (len = headers['content-length'])
        client.read(len.to_i)
      end

      @hits << [method, path]
      write_response(client, method, path)
    rescue StandardError
      # A malformed/aborted connection must not take the server down;
      # just drop this client and keep serving.
    ensure
      begin
        client&.close
      rescue StandardError
        # ignore
      end
    end

    # Parse the request line + headers. Returns [method, path, headers]
    # or [nil, nil, nil] on EOF / malformed input.
    def read_request_head(client)
      request_line = client.gets
      return [nil, nil, nil] if request_line.nil?

      method, path, = request_line.split(' ')
      headers = {}
      while (line = client.gets)
        break if line == "\r\n" || line == "\n"

        key, value = line.split(':', 2)
        headers[key.strip.downcase] = value.strip if value
      end
      [method, path, headers]
    end

    def write_response(client, method, path)
      if method == 'POST' && path == "/apps/tok/symbols"
        # Hand back a presigned PUT URL on THIS same mock server.
        # code:0 (not the SymbolAlreadyExists sentinel) tells the CLI
        # to proceed to the PUT leg.
        put_url   = "http://127.0.0.1:#{@port}/put/x"
        body      = JSON.dump('code' => 0, 'endpoint' => put_url)
        write_http(client, 200, body, 'application/json')
      elsif method == 'PUT'
        # The presigned PUT. Body already drained above. Just 200.
        write_http(client, 200, '')
      else
        write_http(client, 404, '')
      end
    end

    def write_http(client, status, body, content_type = nil)
      reason = status == 200 ? 'OK' : 'Not Found'
      lines  = ["HTTP/1.1 #{status} #{reason}"]
      lines << "Content-Type: #{content_type}" if content_type
      lines << "Content-Length: #{body.bytesize}"
      lines << 'Connection: close'
      client.write(lines.join("\r\n") + "\r\n\r\n" + body)
    end
  end

  # Path to the BugseeAgent helper shipped in the repo root.
  let(:agent_path) { File.expand_path('../../../BugseeAgent', __FILE__) }

  # A minimal valid ProGuard mapping — one renamed class is enough for
  # the CLI's `proguard::identify` to compute a debug-id and pack a zip.
  let(:tmp_mapping) do
    f = Tempfile.new(['mapping', '.txt'])
    f.write("com.example.Foo -> a.b.c:\n")
    f.close
    f
  end

  before(:all) do
    cli = ENV['BUGSEE_CLI_PATH']
    unless cli && File.file?(cli) && File.executable?(cli)
      skip "BUGSEE_CLI_PATH is not set to an executable bugsee-cli binary " \
           "(set it and RUN_INTEGRATION=1 to run the round-trip test)"
    end
  end

  before do
    # Keep the test log readable; the action emits chatty UI output and
    # the CLI failure path (if any) would otherwise spam stderr.
    allow(FastlaneCore::UI).to receive(:error)
    allow(FastlaneCore::UI).to receive(:important)
    allow(FastlaneCore::UI).to receive(:message)
  end

  after do
    tmp_mapping.unlink if File.exist?(tmp_mapping.path)
  end

  it 'performs the real two-stage presigned upload (POST metadata + PUT payload)' do
    # Under RSpec, fastlane considers itself in "test mode"
    # (FastlaneCore::Helper.test? is true because SpecHelper is
    # defined) and Actions.sh becomes a NO-OP that merely echoes the
    # command instead of spawning it. fastlane's own escape hatch for
    # genuine end-to-end tests is FORCE_SH_DURING_TESTS — setting it
    # flips Helper.sh_enabled? back to true so the BugseeAgent (and the
    # real bugsee-cli underneath it) actually runs. Saved/restored so we
    # don't leak the override into sibling specs.
    prev_force_sh = ENV['FORCE_SH_DURING_TESTS']
    ENV['FORCE_SH_DURING_TESTS'] = '1'

    server = MockSymbolServer.new.start
    begin
      # NOTE: the action SWALLOWS CLI failures (rescue -> UI.error), so
      # we never assert on a raised error. We assert on what reached the
      # mock — that's the real signal the round-trip completed.
      described_class.run(
        app_token: 'tok',
        mapping_path: tmp_mapping.path,
        version: '1.0',
        build: '1',
        uuid: '00000000-0000-0000-0000-000000000000',
        host: "http://127.0.0.1:#{server.port}",
        cli_path: ENV['BUGSEE_CLI_PATH'],
        agent_path: agent_path,
      )

      hits = server.recorded_hits

      # Stage 1: the metadata POST landed on the right path.
      expect(hits).to include(['POST', '/apps/tok/symbols'])

      # Stage 2: a PUT (the presigned payload upload) landed too.
      put_hits = hits.select { |method, _path| method == 'PUT' }
      expect(put_hits).not_to be_empty,
                              "expected a PUT (presigned payload upload) but only saw: #{hits.inspect}"
    ensure
      server.stop
      if prev_force_sh.nil?
        ENV.delete('FORCE_SH_DURING_TESTS')
      else
        ENV['FORCE_SH_DURING_TESTS'] = prev_force_sh
      end
    end
  end
end
